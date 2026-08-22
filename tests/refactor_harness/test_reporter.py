"""HarnessReporter.build_report() 的 module 欄位透傳測試。

comparator.py／mutation_verifier.py 現在都會在每筆 result 附上 module
（見同目錄的 test_comparator.py／test_mutation_verifier.py），但
build_report() 是否真的把它原樣放進 failures 裡，之前完全沒有測試覆蓋——
只靠 f.get("module") 這行程式碼本身「看起來對」，沒有斷言鎖住。
"""
from refactor_harness.core.reporter import HarnessReporter


def test_build_report_includes_module_in_failures():
    results = [
        {
            "case_id": "get_version_GET_api_general_version",
            "module": "school",
            "passed": False,
            "error": "golden_not_found",
            "related_files": ["app/routers/school_router.py"],
        }
    ]

    report = HarnessReporter().build_report(results)

    assert len(report["failures"]) == 1
    assert report["failures"][0]["module"] == "school"


def test_build_report_module_defaults_to_none_when_missing():
    """既有呼叫端（若有遺漏補上 module 的舊資料）不應因為缺這個 key 而
    拋例外——build_report() 用 .get() 讀取，缺欄位時要落回 None。"""
    results = [
        {
            "case_id": "some_case",
            "passed": False,
            "error": "golden_not_found",
        }
    ]

    report = HarnessReporter().build_report(results)

    assert report["failures"][0]["module"] is None
