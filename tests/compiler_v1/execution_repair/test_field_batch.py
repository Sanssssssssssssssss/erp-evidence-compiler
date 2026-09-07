"""Batching changes the wire interaction, never the admitted claims or boundaries."""
import asyncio
import copy
import json

import pytest

from app.compiler_runtime.runtime import _sandbox_tools
from test_input_contracts import sandbox


@pytest.mark.parametrize('defect', ['', 'revision', 'fingerprint', 'scope', 'forged_value'])
def test_batch_matches_single_field_validation_and_retains_successful_rows(defect):
    state, single = sandbox(), sandbox()
    events = []
    tool = next(t for t in _sandbox_tools(state, reference_ids_only=True,
        allowed_source_ids=frozenset({'doc'} if defect == 'scope' else {'record'}),
        progress_sink=lambda name, result: events.append((name, copy.deepcopy(result))))
        if t.name == 'bind_record_fields')
    names = {t.name for t in _sandbox_tools(state, reference_ids_only=True)}
    assert 'bind_record_field_claim' not in names
    assert 'bind_record_field_claim' in {t.name for t in _sandbox_tools(state, record_fields_only=True)}
    assert 'bind_record_fields' not in {t.name for t in _sandbox_tools(state, resolver_only=True)}
    args = dict(record_ref='record', record_revision='old' if defect == 'revision' else 'r1', fields=[
        dict(field_path='/amount', predicate='amount', attributes={'currency': 'USD'}, claim_id='observed'),
        dict(field_path='/missing', predicate='missing'),
        dict(field_path='not/a/pointer', predicate='invalid'),
        dict(field_path='/amount', predicate='amount', attributes={'currency': 'USD'}, claim_id='ignored_alias'),
    ])
    if defect == 'fingerprint':
        state._base_ir.source_fingerprints['record'] = 'tampered'
        single._base_ir.source_fingerprints['record'] = 'tampered'
    if defect == 'forged_value':
        args['fields'][1]['value'] = 1
        rejected = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(args))))
        assert not rejected['ok']
        assert not state.evidence_ir.claims and not events
        return
    result = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(args))))
    if defect == 'scope':
        assert result['error']['code'] == 'SOURCE_OUT_OF_SCOPE'
        assert not state.evidence_ir.claims
        assert events[-1][1]['error']['code'] == 'SOURCE_OUT_OF_SCOPE'
        return
    expected = [single.bind_record_field_claim(source_id='record', subject='record',
        locator=dict(record_ref=args['record_ref'], record_revision=args['record_revision'], field_path=f['field_path']),
        **{k: v for k, v in f.items() if k != 'field_path'}) for f in args['fields']]
    assert result['ok'] and len(result['results']) == len(expected)
    for row, canonical, field in zip(result['results'], expected, args['fields']):
        assert row['field_path'] == field['field_path']
        if canonical['ok']:
            full = canonical['claim']
            assert row['claim'] == {k: full[k] for k in ['id', 'predicate', 'value', 'confidence', 'attributes']}
            assert row['claim']['value'] == 90.25 and isinstance(row['claim']['value'], float)
            assert row['claim']['attributes'] == {'currency': 'USD'}
        else:
            assert row['error'] == json.loads(json.dumps(canonical['error'], default=str))
    assert state.evidence_ir.model_dump(mode='json') == single.evidence_ir.model_dump(mode='json')
    if not defect:
        assert [r['ok'] for r in result['results']] == [True, False, False, True]
        assert result['results'][-1]['duplicate'] and result['results'][-1]['claim']['id'] == 'observed'
        assert events[1][1]['claim']['locator']['field_path'] == '/amount'
        retry = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(args))))
        assert len(state.evidence_ir.claims) == 1 and retry['results'][0]['duplicate']
