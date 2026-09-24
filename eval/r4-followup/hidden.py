"""Held-out clamp checks; development checks retain the original frozen semantics."""
import os
import runpy
import sys
from pathlib import Path

if os.environ["R4_CASE"] != "pair":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "r4/hidden.py"), run_name="__main__")
else:
    sys.path.insert(0, os.environ["EVAL_WORKSPACE"])
    from app import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    for values, lower, upper in [([-3, 0, 1, 5, 7], 0, 5), ([-8, -4, -2], -6, -3),
                                  ([0, 2, 4, 2], 2, 2), ([4] * 20, 0, 4), ([1], 0, 3)]:
        expected = [max(lower, min(upper, v)) for v in values]
        r = client.post('/clamp', json={'values': values, 'lower': lower, 'upper': upper})
        assert r.status_code == 200, r.text
        assert r.json() == {'values': expected, 'changed': sum(a != b for a, b in zip(values, expected))}, r.text
    good = {'values': [1], 'lower': 0, 'upper': 2}
    invalid = [{}, {'values': [1], 'lower': 4, 'upper': 2}]
    for field in good:
        invalid.append({k: v for k, v in good.items() if k != field})
        for value in [None, True, 1.5, '1']:
            invalid.append({**good, field: value})
    for value in [[], [1] * 21, [True], [1.0], ['1'], [None], {}, [[1]]]:
        invalid.append({**good, 'values': value})
    for payload in invalid:
        r = client.post('/clamp', json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
    print('pair: trusted clamp HTTP acceptance passed')
