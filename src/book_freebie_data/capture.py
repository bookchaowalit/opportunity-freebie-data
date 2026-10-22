"""One explicit official source; all unsupported facts remain unknown."""

import argparse
import base64
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .cli import resource, validate
from .observability import init_sentry, report_exit


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def _normalize_generic(raw, source, checked_at):
    if source.get("access") == "govuk-api":
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("details"), dict):
            raise ValueError("unexpected GOV.UK document")
        text = " ".join(str(body.get(key, "")) for key in ("title", "description", "details"))
    else:
        parser = Text()
        parser.feed(raw.decode("utf-8"))
        text = " ".join(" ".join(parser.parts).split())
    folded = text.casefold()
    markers = source.get("required_markers") or [source["title"]]
    if not all(marker.casefold() in folded for marker in markers):
        raise ValueError("generic freebie source drift or challenge page")
    schema = resource("schema-v2.json")
    terms: dict[str, Any] = {
        key: None for key in schema["properties"]["items"]["items"]["properties"]["terms"]["properties"]
    }
    terms.update(source.get("normalized_terms", {}))
    record_id = source["id"]
    item = {
        "id": record_id,
        "title": source["title"],
        "record_kind": source["record_kind"],
        "category": source["category"],
        "source_url": source["source_url"],
        "provider": source["provider"],
        "status": source.get("status", "unknown"),
        "deadline": {"kind": source.get("deadline_kind", "unknown"), "at": None},
        "eligible_countries": source.get("eligible_countries", ["GLOBAL"]),
        "eligibility_text": source["eligibility_text"],
        "value": {
            "kind": source.get("value_kind", "access"),
            "amount": source.get("value_amount"),
            "currency": source.get("value_currency"),
            "guaranteed": source.get("value_guaranteed"),
        },
        "cost": {"fee_amount": None, "currency": None, "auto_renewal": None, "notes": source["cost_notes"]},
        "terms": terms,
        "related_records": [],
        "provenance": {
            "evidence_kind": "captured",
            "source_id": source["id"],
            "source_record_id": record_id,
            "checked_at": checked_at,
            "raw_object_key": None,
            "bronze_manifest_key": None,
        },
    }
    return {
        "schema_version": source["schema_version"],
        "source": source["product"],
        "retrieved_at": checked_at,
        "data_status": "live",
        "items": [item],
    }


def normalize(raw, source, checked_at):
    if source.get("generic_html"):
        return _normalize_generic(raw, source, checked_at)
    schema = resource("schema-v2.json")
    terms: dict[str, Any] = {
        key: None for key in schema["properties"]["items"]["items"]["properties"]["terms"]["properties"]
    }
    deadline: dict[str, Any] = {"kind": "unknown", "at": None}
    status = "unknown"
    countries: list[str] = []
    if source["access"] == "github-api":
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("unexpected GitHub content")
        if body.get("encoding") != "base64" or body.get("name") != "about-github-education-for-students.md":
            raise ValueError("unexpected GitHub content")
        text = base64.b64decode("".join(body["content"].split()), validate=True).decode("utf-8")
        if "title: About GitHub Education for students" not in text or "student" not in text.lower():
            raise ValueError("student benefits source drift")
        title = "GitHub Education student benefits"
        terms["delivery_kind"] = "access"
        eligibility = (
            "Student verification and current GitHub Education requirements apply; partner offers have separate terms."
        )
    elif source["access"] == "jetbrains-api":
        body = json.loads(raw)
        article = body.get("article") if isinstance(body, dict) else None
        if not isinstance(article, dict) or article.get("id") != source.get("article_id"):
            raise ValueError("unexpected JetBrains Help Center article")
        if article.get("draft") is not False or article.get("locale") != "en-gb":
            raise ValueError("JetBrains article is not a published English source")
        article_body = article.get("body")
        if not isinstance(article_body, str):
            raise ValueError("JetBrains article body missing")
        parser = Text()
        parser.feed(article_body)
        text = " ".join(" ".join(parser.parts).split())
        required = ("students", "teachers", "free", "commercial")
        if article.get("name") != source.get("expected_article_title") or not all(
            marker in text.lower() for marker in required
        ):
            raise ValueError("JetBrains education source drift")
        title = "JetBrains Student and Teacher Pack"
        terms["delivery_kind"] = "digital"
        terms["redemption_steps"] = (
            "Apply through JetBrains education flow; current verification and account steps must be confirmed at the linked official page."
        )
        eligibility = "Students and teachers can request a free JetBrains Student or Teacher Pack; educational licenses cannot be used for commercial work. Verify the current program requirements."
    else:
        raise ValueError("unknown freebie source")
    record_id = source["id"] + (":" + deadline["at"][:10] if deadline["kind"] == "fixed" else "")
    item = {
        "id": record_id,
        "title": title,
        "record_kind": source["record_kind"],
        "category": source["category"],
        "source_url": source["source_url"],
        "provider": source["provider"],
        "status": status,
        "deadline": deadline,
        "eligible_countries": countries,
        "eligibility_text": eligibility,
        "value": {
            "kind": "access" if source["domain"] == "freebie" else "unknown",
            "amount": None,
            "currency": None,
            "guaranteed": None,
        },
        "cost": {
            "fee_amount": None,
            "currency": None,
            "auto_renewal": None,
            "notes": "Not extracted; review official terms before applying or claiming.",
        },
        "terms": terms,
        "related_records": [],
        "provenance": {
            "evidence_kind": "captured",
            "source_id": source["id"],
            "source_record_id": record_id,
            "checked_at": checked_at,
            "raw_object_key": None,
            "bronze_manifest_key": None,
        },
    }
    return {
        "schema_version": source["schema_version"],
        "source": source["product"],
        "retrieved_at": checked_at,
        "data_status": "live",
        "items": [item],
    }


def source_config(source_id=None):
    if source_id is None:
        return resource("source.json")
    if not re.fullmatch(r"[a-z0-9-]+", source_id):
        raise ValueError("invalid source id")
    base = resource("source.json")
    if base["id"] == source_id:
        return base
    try:
        return resource("source-" + source_id + ".json")
    except FileNotFoundError:
        raise ValueError("unknown source id") from None


def main(argv=None):
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument("--source-id")
    selected, remaining = selector.parse_known_args(argv)
    root = os.environ.get("SOLO_DATA_LAKE_ROOT")
    if not root:
        print("Set SOLO_DATA_LAKE_ROOT to the local shared capture/lake runtime.", file=sys.stderr)
        return 2
    runtime = Path(root).resolve() / "infra/scripts"
    if not (runtime / "discovery/capture_runtime.py").is_file():
        print("Shared discovery runtime is unavailable.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(runtime))
    from discovery.capture_runtime import VERSION, run

    if VERSION != 1:
        raise RuntimeError("unsupported shared capture runtime")
    source = source_config(selected.source_id)
    sentry = init_sentry(source["id"])
    return report_exit(sentry, source["id"], run(source, normalize, validate, remaining))


if __name__ == "__main__":
    raise SystemExit(main())
