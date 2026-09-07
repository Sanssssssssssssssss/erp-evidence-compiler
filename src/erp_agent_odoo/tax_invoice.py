"""Alibaba Cloud invoice observations, captured before a review is sealed."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.compiler_runtime.sandbox import SourceRecord
from app.state.persistence import atomic_write_text

INVOICE_MODEL = "cn.tax_invoice"
RECEIPT_MODEL = "external.tax_invoice_verification"
TEMPLATE_ID = "cn_tax_invoice_review.v1"
FIELD_MAP = {
    "invoice_type": "invoiceType", "invoice_code": "invoiceCode",
    "invoice_number": "invoiceNumber", "invoice_date": "invoiceDate",
    "amount_untaxed": "invoiceMoney", "amount_tax": "allTax", "amount_total": "allValoremTax",
    "buyer_name": "purchaserName", "buyer_tax_id": "purchaserTaxpayerNumber",
    "seller_name": "salerName", "seller_tax_id": "salerTaxpayerNumber",
}


def fingerprint(source):
    return hashlib.sha256(source.content.encode("utf-8")).hexdigest()


def response_hash(response):
    return hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def wire(source):
    return dict(source_id=source.source_id, source_content=source.content, evidence_type=source.kind,
                record_model=source.record_model, record_revision=source.record_revision,
                source_fingerprint=fingerprint(source), provenance=dict(source.provenance), already_persisted=True)


def request_fields(fields):
    """Only the documented common VAT types; never guess a missing query value."""
    kind = fields.get("invoice_type")
    if not isinstance(kind, str) or kind not in {"01", "04", "10", "20", "31", "32"}:
        raise ValueError("Unsupported invoice_type; supported: 01,04,10,20,31,32")
    digital = kind in {"31", "32"}
    required = {"invoice_number": ("InvoiceNo", 20 if digital else 8), "invoice_date": ("InvoiceDate", 8)}
    if not digital:
        required["invoice_code"] = ("InvoiceCode", (10, 12))
    if kind in {"04", "10"}:
        required["verify_code"] = ("VerifyCode", 6)
    query = {}
    for field, (parameter, lengths) in required.items():
        value = fields.get(field)
        lengths = (lengths,) if isinstance(lengths, int) else lengths
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value) or len(value) not in lengths:
            raise ValueError(f"Missing, masked or invalid {field}")
        query[parameter] = value
    try:
        datetime.strptime(query["InvoiceDate"], "%Y%m%d")
    except ValueError:
        raise ValueError("Invalid invoice_date") from None
    if kind not in {"04", "10"}:
        field = "amount_total" if digital else "amount_untaxed"
        value = fields.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            raise ValueError(f"Missing, masked or invalid {field}")
        query["InvoiceSum"] = value
    return query


def _call_aliyun(query):
    # Optional SDK owns authentication/signing; secrets never become evidence.
    from alibabacloud_ocr_api20210707.client import Client
    from alibabacloud_ocr_api20210707.models import VerifyVATInvoiceRequest
    from alibabacloud_tea_openapi.models import Config
    from alibabacloud_tea_util.models import RuntimeOptions

    config = Config(access_key_id=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
                    access_key_secret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
                    security_token=os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN"))
    config.endpoint = "ocr-api.cn-hangzhou.aliyuncs.com"
    request = VerifyVATInvoiceRequest().from_map(query)
    return Client(config).verify_vatinvoice_with_options(request, RuntimeOptions(
        autoretry=False, connect_timeout=5000, read_timeout=15000)).body.to_map()


def receipt_source(invoice, response, *, mode, observed_at, query=None, raw_ref="", error=""):
    if mode not in {"live", "documentation"}:
        raise ValueError("Unknown receipt mode")
    data = response.get("Data", {}) if isinstance(response, dict) else None
    try:
        if isinstance(data, str):
            data = json.loads(data, parse_float=str)
        if not isinstance(data, dict) or (data.get("data") is not None and not isinstance(data["data"], dict)):
            raise ValueError
    except (ValueError, TypeError):
        data, error = {}, "MALFORMED_PROVIDER_RESPONSE"
    code = str(data.get("code", ""))
    details = data.get("data") or {}
    state = {"001": "MATCH", "006": "MISMATCH", "009": "NOT_FOUND"}.get(code, "UNAVAILABLE")
    if state == "MATCH" and not details:
        state, error = "UNAVAILABLE", "EMPTY_PROVIDER_INVOICE"
    values = {field: details[key] for field, key in FIELD_MAP.items() if key in details}
    if "invoice_type" in values:
        values["invoice_type"] = str(values["invoice_type"]).zfill(2)
    if error:
        state = "UNAVAILABLE"
    fields = dict(invoice_source_id=invoice.source_id, invoice_fingerprint=fingerprint(invoice),
                  provider="aliyun.VerifyVATInvoice", mode=mode, observed_at=observed_at,
                  verification_state="DOCUMENTATION_ONLY" if mode == "documentation" else state,
                  provider_code=code, provider_message=data.get("msg", ""), request=query or {},
                  invoice=values, raw_response_ref=raw_ref, raw_response_sha256=response_hash(response), error=error,
                  request_id=response.get("RequestId", "") if isinstance(response, dict) else "")
    if "invalidMark" in details:
        fields["invalid_mark"] = details["invalidMark"]
    return SourceRecord(source_id="tax-verification:" + fingerprint(invoice), content="", kind="record",
                        record_model=RECEIPT_MODEL, record_revision=observed_at, structured_fields=fields,
                        provenance={"role": "evidence", "origin": "aliyun" if mode == "live" else "provider documentation"})


def collect_tax_invoice_sources(sources, directory):
    """Explicit manifest opt-in only. Persist one observation per invoice/run."""
    result = list(sources)
    for item in sources:
        provider = item.get("provenance", {}).get("verify_tax_invoice")
        if not provider:
            continue
        if provider != "aliyun" or item.get("record_model") != INVOICE_MODEL:
            raise ValueError("Tax verification requires an admitted cn.tax_invoice and provider aliyun")
        invoice = SourceRecord(source_id=item["source_id"], content=item["source_content"], kind="record",
                               record_model=INVOICE_MODEL, record_revision=item["record_revision"])
        if item.get("source_fingerprint") != fingerprint(invoice):
            raise ValueError("Tax invoice source fingerprint differs")
        path = Path(directory) / (fingerprint(invoice) + ".json")
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            stored = saved["receipt"]
            prior = SourceRecord(source_id=stored["source_id"], content=stored["source_content"], kind="record",
                                 record_model=RECEIPT_MODEL, record_revision=stored["record_revision"])
            if (fingerprint(prior) != stored["source_fingerprint"]
                    or prior.record_fields.get("invoice_fingerprint") != fingerprint(invoice)
                    or prior.record_fields.get("invoice_source_id") != invoice.source_id
                    or prior.record_fields.get("raw_response_sha256") != response_hash(saved["raw_response"])):
                raise ValueError("Saved tax verification differs from its evidence hashes; do not repeat the query")
        else:
            query, response, error = {}, {}, ""
            try:
                query = request_fields(invoice.record_fields)
            except ValueError as exc:
                error = str(exc)
            try:
                if error:
                    pass
                elif item.get("provenance", {}).get("example"):
                    error = "EXAMPLE_NOT_LIVE"
                elif not all(os.getenv(key) for key in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")):
                    error = "CREDENTIALS_NOT_CONFIGURED"
                else:
                    response = _call_aliyun(query)
            except Exception as exc:
                # SDK exceptions can embed signed requests. Preserve only the exception type.
                error = type(exc).__name__
            timestamp = datetime.now(timezone.utc).isoformat()
            receipt = receipt_source(invoice, response, mode="live", observed_at=timestamp,
                                     query=query, raw_ref=str(path), error=error)
            saved = dict(receipt=wire(receipt), raw_response=response)
            atomic_write_text(path, json.dumps(saved, ensure_ascii=False, indent=2))
        result.append(saved["receipt"])
    ids = [item["source_id"] for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate tax verification source")
    return result


def compare_tax_invoice(sandbox, node, allowed):
    """Bind both observed sides; return facts and existing calculator witnesses."""
    contract = node.action_contract
    sources = {s.source_id: s for s in sandbox.source_records if s.source_id in allowed}
    targets = [s for s in sources.values() if s.source_id in contract.target_record_refs and s.record_model == INVOICE_MODEL]
    if len(targets) != 1 or len(node.facet_refs) != 1:
        raise ValueError("One in-scope cn.tax_invoice target is required")
    invoice = targets[0]
    if {r.record_revision for r in contract.proposal_records if r.record_ref == invoice.source_id} != {invoice.record_revision}:
        raise ValueError("Invoice source revision differs from the sealed proposal")
    receipts = [s for s in sources.values() if s.record_model == RECEIPT_MODEL
                and s.record_fields.get("invoice_source_id") == invoice.source_id]
    if len(receipts) != 1 or receipts[0].record_fields.get("invoice_fingerprint") != fingerprint(invoice):
        raise ValueError("One receipt bound to this exact invoice snapshot is required")
    receipt = receipts[0]
    facts, rows = {}, []
    def bind(source, pointer):
        sandbox.read_source(source.source_id)
        result = sandbox.bind_record_field_claim(source_id=source.source_id, subject=source.source_id, predicate=pointer,
            locator={"record_ref": source.source_id, "record_revision": source.record_revision, "field_path": pointer})
        if not result["ok"]:
            raise ValueError(result["error"]["code"])
        claim = result["claim"]
        facts[claim["id"]] = claim["value"]
        return claim["id"]
    for key in ("provider", "mode", "verification_state", "provider_code", "observed_at", "invoice_fingerprint",
                "raw_response_sha256", "error", "invalid_mark", "request", "request_id", "invoice_source_id"):
        if key in receipt.record_fields:
            bind(receipt, "/" + key)
    for field in FIELD_MAP:
        refs = []
        for source, values, prefix in ((invoice, invoice.record_fields, "/"),
                                       (receipt, receipt.record_fields.get("invoice", {}), "/invoice/")):
            refs.append(bind(source, prefix + field) if field in values and values[field] not in (None, "") else None)
        row = dict(field=field, claim_ids=refs, equal=None)
        if not all(refs):
            row["gap"] = "Field missing on one or both sides"
        if all(refs):
            if field.startswith("amount_"):
                try:
                    if not all(Decimal(str(facts[ref])).is_finite() for ref in refs):
                        raise InvalidOperation
                    computed = sandbox.compute_witness(check_id=node.id, facet_ref=node.facet_refs[0], operation="EQUAL", refs=refs)
                    if not computed["ok"]:
                        raise ValueError(computed["error"]["code"])
                    row.update(equal=computed["witness"]["result"], witness_id=computed["witness"]["id"])
                except InvalidOperation:
                    row["gap"] = "Amount is masked or not a finite decimal"
            else:
                row["equal"] = type(facts[refs[0]]) is type(facts[refs[1]]) and facts[refs[0]] == facts[refs[1]]
        rows.append(row)
    return dict(ok=True, receipt_source_id=receipt.source_id, verification_state=receipt.record_fields.get("verification_state"),
                claims=facts, comparisons=rows, instruction="These are observed comparisons, not an approval. Documentation is not live verification; missing values and lookup failures remain evidence gaps.")
