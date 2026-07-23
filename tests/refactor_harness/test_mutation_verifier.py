"""
MutationVerifier 排除 tainted folder 的測試（見 docs/02a_harness_architecture.md
四章「排除已知異常的 folder」）。

DbEnvironment.apply_seed 與 core.postman_runner.run_newman／
list_top_level_folders 全部 mock 掉，不連真實 DB、不執行真實 newman。
"""
import json

from refactor_harness.fixtures.db_env import DbEnvironment
from refactor_harness.verifier import mutation_verifier
from refactor_harness.verifier.mutation_verifier import MutationVerifier

_TAINTED_FOLDER = "order_lifecycle_bad"
_CLEAN_FOLDER = "order_lifecycle_ok"


def _write_metadata(golden_dir, tainted_folders):
    metadata = {
        "recorded_at": "2026-01-01T00:00:00Z",
        "total_cases": 0,
        "cases": [],
        "skipped": [],
        "tainted_folders": tainted_folders,
    }
    with open(golden_dir / "_metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


def test_verify_all_raw_skips_tainted_folder(tmp_path, monkeypatch):
    """
    情境 c：
    - verify_all_raw() 執行時完全沒有對 tainted folder 呼叫 newman
    - get_excluded_folders() 回傳該 folder 名稱
    """
    _write_metadata(tmp_path, tainted_folders=[
        {
            "folder": _TAINTED_FOLDER,
            "anomalies": [{"case_id": "x", "status_code": 400, "body_preview": ""}],
            "excluded_case_ids": ["x", "y"],
        }
    ])

    monkeypatch.setattr(DbEnvironment, "apply_seed", lambda self, *a, **kw: None)
    monkeypatch.setattr(
        mutation_verifier, "list_top_level_folders",
        lambda collection_path: [_TAINTED_FOLDER, _CLEAN_FOLDER],
    )

    called_folders = []

    def _fake_run_newman(collection_path, base_url, folder=None):
        called_folders.append(folder)
        assert folder != _TAINTED_FOLDER, "tainted folder 不應被執行 newman"
        return {"run": {"executions": []}}

    monkeypatch.setattr(mutation_verifier, "run_newman", _fake_run_newman)

    verifier = MutationVerifier(
        python_base_url="http://localhost:8000",
        golden_dir=str(tmp_path),
        test_dsn="postgresql://fake/db",
    )
    results = verifier.verify_all_raw()

    assert called_folders == [_CLEAN_FOLDER]
    assert results == []  # clean folder 的 newman 回傳空 executions
    assert verifier.get_excluded_folders() == [_TAINTED_FOLDER]


def test_init_without_metadata_file_is_not_tainted(tmp_path, monkeypatch):
    """_metadata.json 不存在時（初次執行）__init__ 不應拋例外，
    _tainted_folder_names 是空集合。"""
    monkeypatch.setattr(DbEnvironment, "apply_seed", lambda self, *a, **kw: None)

    assert not (tmp_path / "_metadata.json").exists()

    verifier = MutationVerifier(
        python_base_url="http://localhost:8000",
        golden_dir=str(tmp_path),
        test_dsn="postgresql://fake/db",
    )

    assert verifier._tainted_folder_names == set()
