"""Exercise the SDK tool loop: invalid output is feedback, never a supplied verdict."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import Agent, Model, ModelResponse, RunConfig, Runner, Usage
from agents.exceptions import ModelBehaviorError
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from app.compiler_runtime import runtime as module


@pytest.mark.parametrize('mode', ['SUPPORTED', 'CONTRADICTED', 'NOT_FOUND', 'legacy', 'repeat_error', 'plain_text', 'early_submit', 'plan_issue'])
def test_sdk_verifier_submission_and_one_correction(monkeypatch, mode):
    seen = []
    output = module.VerificationBatch if mode == 'legacy' else module.EvidenceVerificationBatch
    expected = mode if mode in {'SUPPORTED', 'CONTRADICTED', 'NOT_FOUND'} else 'NOT_FOUND'
    missing = {'assessments': [{'check_id': 'unseen-check', 'reason': 'This text does not supply a verdict.'}]}
    if mode != 'legacy':
        missing['assessments'][0]['source_scope_reviewed'] = True
    corrected = json.loads(json.dumps(missing))
    corrected['assessments'][0]['status'] = expected

    class ScriptedModel(Model):
        async def get_response(self, **kwargs):
            seen.append(kwargs['input'])
            assert kwargs['output_schema'] is None
            assert kwargs['model_settings'].tool_choice is None
            if len(seen) == 2:
                feedback = json.loads(seen[-1][-1]['output'])
                assert feedback['error']['code'] == 'TOOL_INPUT_INVALID'
                assert any(e['loc'] == ['assessments', 0, 'status'] for e in feedback['error']['details']['validation_errors'])
                assert all('input' not in e for e in feedback['error']['details']['validation_errors'])
            body = missing if len(seen) == 1 or mode == 'repeat_error' else corrected
            if mode == 'plan_issue':
                body = {'assessments': [], 'plan_issue': 'The sealed plan omits an explicit obligation.'}
            if mode in {'early_submit', 'plain_text'}:
                body = corrected
            raw = json.dumps(body)
            item = ResponseFunctionToolCall(type='function_call', name='submit_verification',
                call_id=f'call-{len(seen)}', arguments=raw)
            if mode == 'plain_text':
                item = ResponseOutputMessage(type='message', id='m', role='assistant', status='completed',
                    content=[ResponseOutputText(type='output_text', text=raw, annotations=[])])
            return ModelResponse(output=[item], usage=Usage(requests=1, input_tokens=10,
                output_tokens=4, total_tokens=14), response_id=f'r{len(seen)}')

        def stream_response(self, **kwargs):
            raise AssertionError('This offline test uses the SDK non-streaming tool loop')

    model = ScriptedModel()
    monkeypatch.setattr(module, 'Agent', lambda **kw: Agent(**{**kw, 'model': model}))
    monkeypatch.setattr(module, 'build_run_config', lambda *_a, **_kw: RunConfig(tracing_disabled=True))
    monkeypatch.setattr(module, 'run_agent_sync', lambda agent, data, **kw: asyncio.run(
        Runner.run(agent, data, max_turns=kw['max_turns'], run_config=kw['run_config'])))
    settings = SimpleNamespace(llm_model='offline', llm_temperature=0, llm_thinking_type='high',
        llm_base_url='https://api.commandcode.ai/provider/v1')
    runtime = module.EvidenceCompilerRuntime(SimpleNamespace(available=True, settings=settings, calls=[]), settings=settings)
    async def reveal(_ctx, _raw):
        return json.dumps({'ok': True, 'candidate': {}})
    tools = [module._function_tool('reveal_candidate', 'Offline source-first boundary',
        module._RevealCandidateInput, reveal)] if mode in {'early_submit', 'plan_issue'} else []
    args = dict(name='fine_verifier', prompt_file='evidence_verifier.md',
        prompt_version_key='evidence_verifier', payload={}, output_type=output, tools=tools, max_turns=None)
    if mode in {'repeat_error', 'plain_text', 'early_submit'}:
        with pytest.raises(ModelBehaviorError, match={'repeat_error': 'one submission correction',
            'plain_text': 'real tools', 'early_submit': 'prior turn'}[mode]):
            runtime._run_phase(**args)
        assert len(seen) == (2 if mode == 'repeat_error' else 1)
        assert runtime.llm.calls[0].error
    else:
        result = runtime._run_phase(**args)
        if mode == 'plan_issue':
            assert result.plan_issue and not result.assessments
        else:
            assert result.assessments[0].status == expected and len(seen) == 2
        assert not runtime.llm.calls[0].error
        assert runtime.llm.calls[0].provider_turn_count == len(seen)
