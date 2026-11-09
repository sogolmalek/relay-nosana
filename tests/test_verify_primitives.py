"""relay_verify primitives: base58, CIDv0, Nosana job account decoding (no network)."""

import base64
import hashlib
import struct

import pytest

from relay_verify.b58 import b58decode, b58encode
from relay_verify.cid import cid_to_digest, cidv0_of_bytes, digest_to_cid
from relay_verify.solana import (
    FINISH_DISCRIMINATOR,
    JOB_ACCOUNT_DISCRIMINATOR,
    LIST_DISCRIMINATOR,
    NOSANA_JOBS_PROGRAM,
    decode_assign_ix,
    decode_finish_ix,
    decode_job_account,
    decode_list_ix,
    job_from_rpc_response,
)


def test_b58_roundtrip_and_leading_zeros():
    for data in [b"", b"\x00", b"\x00\x00abc", bytes(range(32)), b"\xff" * 32]:
        assert b58decode(b58encode(data)) == data
    assert b58encode(b"\x00\x00\x01") == "112"


def test_b58_known_program_id_is_32_bytes():
    raw = b58decode(NOSANA_JOBS_PROGRAM)
    assert len(raw) == 32
    assert b58encode(raw) == NOSANA_JOBS_PROGRAM


def test_cidv0_known_vector_hello_world():
    # `echo "hello world" | ipfs add` -> QmT78zSuBmuS4z925WZfrqQ1qHaJ56DQaTfyMUF7F8ff5o
    assert cidv0_of_bytes(b"hello world\n") == "QmT78zSuBmuS4z925WZfrqQ1qHaJ56DQaTfyMUF7F8ff5o"


def test_cid_digest_roundtrip_and_change_detected():
    cid = cidv0_of_bytes(b'{"a":1}')
    assert digest_to_cid(cid_to_digest(cid)) == cid
    assert cidv0_of_bytes(b'{"a":2}') != cid


def test_cid_rejects_multiblock():
    with pytest.raises(ValueError):
        cidv0_of_bytes(b"x" * (262144 + 1))


def _fake_job_bytes(state=2, t_start=1_700_000_000, t_end=1_700_000_300):
    pk = lambda i: bytes([i]) * 32  # noqa: E731
    job_digest = cid_to_digest(cidv0_of_bytes(b"jobdef"))
    res_digest = cid_to_digest(cidv0_of_bytes(b"result"))
    data = (
        JOB_ACCOUNT_DISCRIMINATOR + job_digest + res_digest + pk(1) + pk(2) + pk(3)
        + struct.pack("<Q", 12345) + pk(4) + bytes([state])
        + struct.pack("<qqq", t_end, t_start, 3600)
    )
    assert len(data) == 233
    return data


def test_decode_job_account_fields():
    job = decode_job_account("J", _fake_job_bytes())
    assert job.ipfs_job == cidv0_of_bytes(b"jobdef")
    assert job.ipfs_result == cidv0_of_bytes(b"result")
    assert job.market == b58encode(bytes([1]) * 32)
    assert job.node == b58encode(bytes([2]) * 32)
    assert job.payer == b58encode(bytes([3]) * 32)
    assert job.project == b58encode(bytes([4]) * 32)
    assert (job.price, job.state_name, job.time_start, job.time_end, job.timeout) == (
        12345, "COMPLETED", 1_700_000_000, 1_700_000_300, 3600)


def test_unfinished_job_has_no_result_cid():
    data = bytearray(_fake_job_bytes(state=1))
    data[40:72] = b"\x00" * 32
    assert decode_job_account("J", bytes(data)).ipfs_result is None


def test_wrong_discriminator_or_owner_rejected():
    data = bytearray(_fake_job_bytes())
    data[0] ^= 1
    with pytest.raises(ValueError):
        decode_job_account("J", bytes(data))
    resp = {"result": {"value": {"owner": "11111111111111111111111111111111",
                                 "data": [base64.b64encode(_fake_job_bytes()).decode(), "base64"]}}}
    with pytest.raises(ValueError):
        job_from_rpc_response("J", resp)


def test_missing_account_is_none():
    assert job_from_rpc_response("J", {"result": {"value": None}}) is None


def test_decode_list_and_finish_instructions():
    d = hashlib.sha256(b"x").digest()
    lst = decode_list_ix(LIST_DISCRIMINATOR + d + struct.pack("<q", 3600))
    assert lst == {"ipfs_job": digest_to_cid(d), "timeout": 3600}
    assert decode_finish_ix(FINISH_DISCRIMINATOR + d) == {"ipfs_result": digest_to_cid(d)}
    assert decode_finish_ix(LIST_DISCRIMINATOR + d) is None


def test_decode_assign_ix_from_real_phase0_tx():
    # Node-targeted rerun job 959C21mq..., tx 2aferSZz...: created by `assign`, not `list`.
    data = b58decode("3gsF6BneLMztB2ZscfvrXNaYSif2b5zoNjy8nbda3TvvzixmAwq7cnUrskLNYkRWiK")
    assert decode_assign_ix(data) == {"ipfs_job": "QmNdyDQtZLewTM8NcLrsQyW1NEPKscMamktDr3bYLKQsii", "timeout": 3600}
    assert decode_list_ix(data) is None and decode_finish_ix(data) is None
    assert data[:8] == hashlib.sha256(b"global:assign").digest()[:8]
