from __future__ import annotations

import hashlib
import json
from typing import Iterable, Optional
from datetime import datetime , UTC
import networkx as nx 
import jwt
from jwt import PyJWTError
import os 

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def _load_or_generate_signing_key():
    raw = os.environ.get("SIGNING_KEY_PEM")
    if raw:
        return serialization.load_pem_private_key(raw.encode(), password=None)
    key = Ed25519PrivateKey.generate()
    os.environ["SIGNING_KEY_PEM"] = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key


signing_key = _load_or_generate_signing_key()

algorithm = "EdDSA"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()

def iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000)


def short_id(full_id: str, n: int = 8) -> str:
    return full_id[:n]

def short_labels(events: Iterable) -> dict[str, str]:
    """{event_id: 'E1'|'E2'|...} in iteration order."""
    return {e.event_id: f"E{i}" for i, e in enumerate(events, start=1)}


def hash_payload(payload: dict, length: int = 16) -> str:
    payload_str = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(payload_str.encode()).hexdigest()[:length]

def hash_task(task:str, length:int=16):
    return hashlib.sha256(task.encode()).hexdigest()[:length]


class ContractViolation(RuntimeError):
    """Raised when a CONTRACT v1 invariant is broken at runtime.

    Carries `contract_num` so a failing test points a reviewer
    straight at CONTRACTS.md#N.
    """

    def __init__(self, contract_num: int, detail: str = "") -> None:
        self.contract_num = contract_num
        self.detail = detail
        msg = f"CONTRACT v1 #{contract_num} violated"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


def enforce(condition: bool, contract_num: int, detail: str = "") -> None:
    """Raise ContractViolation(contract_num, detail) if condition is False.

    Example:
        enforce(cause in store, 3, f"caused_by={cause!r} not in store")
    """
    if not condition:
        raise ContractViolation(contract_num, detail)


def assert_dag(g):
    if not nx.is_directed_acyclic_graph(g):
        raise ContractViolation(4, "causal graph is not a DAG")


def causes_only(g: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """Subgraph view containing only 'causes' edges.

    'contains' edges are structural (span hierarchy) and must be excluded
    from any shortest/longest-path query over causation.
    """
    return nx.subgraph_view(
        g,
        filter_edge=lambda u, v, k: g[u][v][k].get("type") == "causes",
    )



def make_delegation_token(
    issuer: str,
    subject: str,
    audience: str,
    session_id: str,
    task: str,
    ttl_seconds: int = 300,

) -> str:
    """
    iss   = issuer agent id
    sub   = subject agent id
    aud   = scope (e.g. "tool:lookup_data")
    sid   = session id
    th    = hash_task(task)
    iat   = issued-at epoch seconds
    exp   = iat + ttl_seconds

    """
    now = datetime.now(UTC).timestamp()
    payload =   {
        "iss": issuer,             
        "sub": subject,             
        "aud": audience,     
        "sid": session_id,
        "task_hash": hash_task(task),     
        "iat": now, 
        "exp": now + ttl_seconds, 
    }
    return jwt.encode(
        payload,
        key=signing_key,
        algorithm=algorithm,
    )


def verify_delegation_token(
    token: str,
    expected_subject: str,
    expected_session: str,
    expected_task: str,
    expected_audience: str,
    expected_issuer: Optional[str] = None,

) -> bool:
    public_key = signing_key.public_key()

    try:
        decoded = jwt.decode(
            token,
            key=public_key,
            algorithms=[algorithm],
            audience=expected_audience,
        )
    except PyJWTError:
        return False

    if decoded.get("sub") != expected_subject:
        return False
    if decoded.get("sid") != expected_session:
        return False
    if decoded.get("task_hash") != hash_task(expected_task):
        return False
    if expected_issuer is not None and decoded.get("iss") != expected_issuer:
        return False
    return True

