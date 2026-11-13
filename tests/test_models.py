"""The models against the API's own document, and the client that waits against the one that awaits."""

from __future__ import annotations

import dataclasses
import inspect
import json
import subprocess
import sys
import types
import typing
from pathlib import Path
from typing import Any

import pytest

import agental
from agental import Agental, AsyncAgental, AsyncSandbox, Sandbox, _async, _sync, models
from agental.models import _camel

ROOT = Path(__file__).resolve().parent.parent
# The document is beside the SDK where the API is developed, and not where the SDK is published from.
DOCUMENT = ROOT.parent.parent / "openapi.json"

NAMED = {
    "Sandbox": models.SandboxInfo,
    "SandboxAutoStop": models.SandboxAutoStop,
    "ExecResult": models.ExecResult,
    "Command": models.Command,
    "Snapshot": models.Snapshot,
    "HostRoute": models.HostRoute,
    "DesktopState": models.DesktopState,
    "BrowserState": models.BrowserState,
    "FileWritten": models.FileWritten,
    "Catalog": models.Catalog,
    "Policy": models.Policy,
    "SandboxUsage": models.SandboxUsage,
    "Usage": models.Usage,
    "SandboxEmail": models.SandboxEmail,
    "EmailMessageSummary": models.EmailMessageSummary,
    "EmailMessage": models.EmailMessage,
    "EmailMessageList": models.EmailMessageList,
    "AccountEmail": models.AccountEmail,
    # What is sent, where the others are what is answered: the SDK offers every field the API takes, and no other.
    "SendEmail": models.SendEmail,
    "Template": models.TemplateInfo,
    "PlainVariable": models.PlainVariable,
    "HiddenVariable": models.HiddenVariable,
    "TemplateStart": models.TemplateStart,
    "TemplateNetwork": models.TemplateNetwork,
    "SandboxNetworkDetail": models.SandboxNetworkDetail,
    "SandboxTemplate": models.SandboxTemplate,
    "TemplateApplied": models.TemplateApplied,
    "SandboxMetrics": models.SandboxMetrics,
    "MetricsAtMoment": models.MetricsAtMoment,
    "RunningOverTime": models.RunningOverTime,
    "Telemetry": models.Telemetry,
    "TelemetryTest": models.TelemetryTest,
    "ApiKey": models.ApiKey,
    "ApiKeyUsage": models.ApiKeyUsage,
    "Burst": models.HeadroomNow,
    "Headroom": models.Headroom,
    "BurstQuote": models.BurstQuote,
    "Prices": models.Prices,
    "Billing": models.Billing,
    # What is sent, as SendEmail is.
    "CreateApiKey": models.CreateApiKey,
}


def resolved(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema or "allOf" in schema or "anyOf" in schema:
        if "$ref" in schema:
            schema = schemas[schema["$ref"].rsplit("/", 1)[-1]]
        elif "allOf" in schema:
            schema = schema["allOf"][0]
        else:
            # One thing or null: the thing is what a model is held against.
            schema = next(each for each in schema["anyOf"] if each.get("type") != "null")
    return schema


# What the API still sends for older callers and the SDK does not read: the retired volume discount, and the
# plan's count and burst in the shape tiers had, which running_units and free_burst_units replaced.
RETIRED = {"Prices": {"volumeDiscount"}, "Plan": {"sandboxesAtOnce", "burst"}, "Billing": {"discount", "sandboxesAtOnce"}}


def differences(model: type, schema: dict[str, Any], schemas: dict[str, Any], where: str) -> list[str]:
    schema = resolved(schema, schemas)
    found: list[str] = []
    hints = typing.get_type_hints(model)
    fields = {_camel(field.name): hints[field.name] for field in dataclasses.fields(model)}
    properties = {name: part for name, part in schema["properties"].items() if name not in RETIRED.get(model.__name__, set())}
    if set(fields) != set(properties):
        found.append(f"{where}: the model has {sorted(set(fields) - set(properties))} more and {sorted(set(properties) - set(fields))} less than the API")
    for name, hint in fields.items():
        if name not in properties:
            continue
        target, part = hint, resolved(properties[name], schemas)
        if typing.get_origin(target) in (typing.Union, types.UnionType):
            target = next(each for each in typing.get_args(target) if each is not type(None))
        if typing.get_origin(target) is list:
            target, part = typing.get_args(target)[0], resolved(part["items"], schemas)
        if dataclasses.is_dataclass(target) and isinstance(target, type):
            found += differences(target, part, schemas, f"{where}.{name}")
    return found


@pytest.mark.skipif(not DOCUMENT.exists(), reason="openapi.json is not beside the SDK here")
def test_every_model_names_the_fields_the_api_answers_with() -> None:
    schemas = json.loads(DOCUMENT.read_text())["components"]["schemas"]
    found: list[str] = []
    for name, model in NAMED.items():
        found += differences(model, schemas[name], schemas, name)
    # A template's sandboxes are answered in an object of their own, which the SDK unwraps.
    found += differences(models.TemplateSandbox, schemas["TemplateSandboxes"]["properties"]["sandboxes"]["items"], schemas, "TemplateSandboxes.sandboxes")
    # A key as it is made is a key with its secret, which the document says in two parts.
    key, secret = schemas["CreatedApiKey"]["allOf"]
    found += differences(models.CreatedApiKey, {"properties": {**resolved(key, schemas)["properties"], **secret["properties"]}}, schemas, "CreatedApiKey")
    assert found == []


def test_the_client_that_waits_is_written_from_the_one_that_awaits() -> None:
    checked = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sync.py"), "--check"], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr

    def public(kind: type) -> set[str]:
        return {name for name in dir(kind) if not name.startswith("_")}

    assert public(Sandbox) == public(AsyncSandbox)
    assert public(Agental) - {"close"} == public(AsyncAgental) - {"aclose"}


def test_every_model_and_every_part_of_the_client_is_exported() -> None:
    """What a public method answers with, or a client holds as an attribute, can be named from agental itself."""

    def defined(module: types.ModuleType) -> set[str]:
        return {name for name, value in vars(module).items() if inspect.isclass(value) and value.__module__ == module.__name__ and not name.startswith("_")}

    wanted = defined(models) | defined(_async) | defined(_sync)
    assert {"SizeInfo", "RunningAt", "AsyncSandboxes", "Snapshots", "ApiKeys"} <= wanted
    assert wanted - set(agental.__all__) == set()
    assert all(getattr(agental, name) is not None for name in agental.__all__)
    assert len(agental.__all__) == len(set(agental.__all__))


def test_the_version_is_written_in_one_place() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert 'path = "src/agental/_version.py"' in pyproject
    assert agental.__version__ == _async._USER_AGENT.removeprefix("agental-python/")
