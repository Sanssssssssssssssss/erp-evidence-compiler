"""Generic observation, proof-closure and wire-boundary checks; no ERP case IDs."""
import hashlib
import random
from types import SimpleNamespace

import pytest
from agents.exceptions import ModelBehaviorError

from app.compiler_runtime.models import EvidenceIR, EvidenceSourceDescriptor
from app.compiler_runtime.runtime import (
    EvidenceAssessment, EvidenceVerificationBatch, EvidenceCompilerRuntime,
    ExecutorSummary, _completion_hook, _expand_verified_closures,
)
from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord


def test_auto_observation_preserves_random_values_and_rejects_bad_locators():
    rng = random.Random(50419)
    values = [None, False, 'USD', 6.0, {'a/b': [12, 'x']}]
    values += [rng.uniform(-1000, 1000) for _ in range(10)]
    for value in values:
        source_id = f'unseen:{rng.randrange(10**9)}'
        source = SourceRecord(source_id=source_id, kind='record', content='',
            record_model='unseen.model', record_revision='r8', structured_fields={'value': value})
        fingerprint = hashlib.sha256(source.content.encode()).hexdigest()
        state = EvidenceSandbox(sources=[source], evidence_ir=EvidenceIR(
            source_ids=[source_id], source_fingerprints={source_id: fingerprint},
            source_revisions={source_id: 'r8'}, source_descriptors={source_id: EvidenceSourceDescriptor(
                source_type='record', fingerprint=fingerprint, revision='r8', record_model='unseen.model')}),
            allowed_check_ids=['c'], allowed_check_facets={'c': ['f']}, policy_snapshot_hash='fixed')
        args = dict(subject=source_id, source_id=source_id, predicate='value',
            locator=dict(record_ref=source_id, record_revision='r8', field_path='/value'))
        assert state.bind_record_field_claim(**args)['error']['code'] == 'SOURCE_NOT_READ'
        state.read_source(source_id)
        for field, bad, code in [('record_revision', 'old', 'SOURCE_REVISION_MISMATCH'),
                                 ('record_ref', 'other', 'LOCATOR_RECORD_MISMATCH'),
                                 ('field_path', '/missing', 'LOCATOR_FIELD_NOT_FOUND')]:
            failed = state.bind_record_field_claim(**{**args, 'locator': {**args['locator'], field: bad}})
            assert failed['error']['code'] == code
            assert not state.evidence_ir.claims
        receipt = state.bind_record_field_claim(**args)
        assert receipt['ok'], receipt
        observed = state.evidence_ir.claims[0].value
        assert type(observed) is type(value) and observed == value
        state._base_ir.source_fingerprints[source_id] = 'tampered'
        assert state.bind_record_field_claim(**args)['error']['code'] == 'SOURCE_FINGERPRINT_MISMATCH'


def test_closure_expansion_cannot_cross_checks_or_fabricate_acceptance():
    checks = [{'id': 'c', 'action_contract': {'source_refs': ['s1', 's2']}, 'terminal_closures': [{
        'binding_id': 'b', 'claim_ids': ['x', 'y'], 'witness_ids': ['w1', 'w2'], 'source_ids': ['s1', 's2']} ]},
        {'id': 'other', 'terminal_closures': []}]
    def expand(**kwargs):
        return _expand_verified_closures(EvidenceVerificationBatch(assessments=[EvidenceAssessment(
            check_id='c', status='SUPPORTED', **kwargs)]), checks).assessments[0]
    accepted = expand(accepted_binding_ids=['b'], source_scope_reviewed=True)
    assert accepted.claim_ids == ['x', 'y'] and accepted.accepted_witness_ids == ['w1', 'w2']
    assert accepted.source_ids == ['s1', 's2']
    assert accepted.examined_source_ids == ['s1', 's2']
    unreviewed = expand(accepted_binding_ids=['b'], source_scope_reviewed=False)
    assert not unreviewed.examined_source_ids  # Delivery does not imply examination.
    rejected = expand(accepted_binding_ids=[], source_scope_reviewed=True)
    assert not rejected.claim_ids and not rejected.source_ids and not rejected.accepted_witness_ids
    with pytest.raises(ValueError, match='outside'):
        expand(accepted_binding_ids=['fabricated'], source_scope_reviewed=True)
    with pytest.raises(ValueError, match='outside'):
        _expand_verified_closures(EvidenceVerificationBatch(assessments=[EvidenceAssessment(
            check_id='other', status='SUPPORTED', accepted_binding_ids=['b'], source_scope_reviewed=True)]), checks)


@pytest.mark.parametrize('phase', ['executor', 'fine_verifier', 'task_compiler'])
def test_phase_wire_contract_and_invalid_text_fail_closed(monkeypatch, phase):
    captured = []
    sent = []
    monkeypatch.setattr('app.compiler_runtime.runtime.Agent', lambda **kwargs: captured.append(kwargs) or object())
    monkeypatch.setattr('app.compiler_runtime.runtime.build_run_config', lambda *_a, **_kw: object())
    def run(*args, **kwargs):
        sent.append(kwargs)
        raise ModelBehaviorError('Invalid protocol: <DSML> is text, not a tool call')
    monkeypatch.setattr('app.compiler_runtime.runtime.run_agent_sync', run)
    settings = SimpleNamespace(llm_model='offline', llm_temperature=0, llm_thinking_type='high',
        llm_base_url='https://api.commandcode.ai/provider/v1')
    llm = SimpleNamespace(available=True, settings=settings, calls=[])
    runtime = EvidenceCompilerRuntime(llm, settings=settings)
    output = ExecutorSummary if phase == 'executor' else EvidenceVerificationBatch
    with pytest.raises(ModelBehaviorError):
        runtime._run_phase(name=phase, prompt_file='evidence_executor.md' if phase == 'executor' else 'evidence_verifier.md',
            prompt_version_key='evidence_executor' if phase == 'executor' else 'evidence_verifier',
            payload={}, output_type=output, max_turns=1, tools=[object()] if phase == 'executor' else [])
    assert len(sent) == 1 and sent[0]['stream_response'] is True
    assert len(llm.calls) == 1 and llm.calls[0].error
    if phase == 'executor':
        assert captured[0]['output_type'] is None
        assert captured[0]['model_settings'].tool_choice is None
    elif phase == 'fine_verifier':
        assert captured[0]['output_type'] is None
        assert [t.name for t in captured[0]['tools']] == ['submit_verification']
        assert captured[0]['model_settings'].tool_choice is None
    else:
        assert captured[0]['output_type'].is_strict_json_schema()


@pytest.mark.parametrize('submitted', [False, True])
def test_executor_completion_comes_from_tool_state_not_sdk_text(monkeypatch, submitted):
    from test_input_contracts import sandbox
    state = sandbox()
    monkeypatch.setattr('app.compiler_runtime.runtime.Agent', lambda **_kw: object())
    monkeypatch.setattr('app.compiler_runtime.runtime.build_run_config', lambda *_a, **_kw: object())
    def run(*_args, **_kwargs):
        if submitted:
            assert state.submit_check(check_id='c', note='Authorization missing')['ok']
        return SimpleNamespace(final_output='<DSML>submit_check c COMPLETED</DSML>', raw_responses=[])
    monkeypatch.setattr('app.compiler_runtime.runtime.run_agent_sync', run)
    settings = SimpleNamespace(llm_model='offline', llm_temperature=0, llm_thinking_type='high',
        llm_base_url='https://api.commandcode.ai/provider/v1')
    runtime = EvidenceCompilerRuntime(SimpleNamespace(available=True, settings=settings, calls=[]), settings=settings)
    kwargs = dict(name='executor', prompt_file='evidence_executor.md', prompt_version_key='evidence_executor',
        payload={}, output_type=ExecutorSummary, max_turns=1, tools=[object()],
        tool_use_behavior=_completion_hook(state, ['c']))
    if submitted:
        summary = runtime._run_phase(**kwargs)
        assert summary.execution_status == 'COMPLETED' and summary.unresolved_check_ids == ['c']
    else:
        with pytest.raises(ModelBehaviorError, match='real tools'):
            runtime._run_phase(**kwargs)
        assert not state.submissions
