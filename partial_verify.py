"""局部真實測試（partial real test）進入點：見 00_refactor_architecture.md
十二、「局部真實測試 vs 完整真實測試」。

只做 ⑥（Harness 驗證），完全跳過 ①～⑤／⑦（parse／design／plan／scaffold／
implement／debug）——假設 python_project_path 底下已經有生成好的程式碼
（不論是完整跑過一次 `python main.py`，還是像這次一樣人工直接修正過），
直接啟動 Python 服務容器、對挑選出來的一批 readonly case 跑 golden 驗證，
印出報告。

跟 `main.py`（完整真實測試）的關鍵差異：
- 不呼叫 LangGraph，不產生／不消耗任何 task_list、不呼叫 Ollama／Claude API
- 只驗證「⑥ 拿到的程式碼現在對不對」，不驗證「①～⑤／⑦ 這次生成／修正
  得對不對」——如果 python_project_path 裡的程式碼是這次才手動修正的
  （不是透過 pipeline 重新生成），這裡的結果不能代表 pipeline 本身沒問題
- 不啟動 mutation 驗證（不需要真正寫入資料的測試案例），只跑 readonly
  golden 比對——這是刻意的簡化，讓「先確認服務起得來、簡單案例過不過」
  這件事盡量快，不是要取代完整真實測試涵蓋的 mutation／regression 範圍
- 不含 config_env_vars（③ 輸出的 @Value 注入資料，見 09b_bug_trace.md
  #46）——這裡沒有經過 ③，沒有這份資料可用，容器會在沒有額外注入的
  @Value 環境變數下啟動。若挑選的 case 剛好依賴某個 @Value 設定值，這裡
  驗證不到，需要改用完整真實測試

用法（見 --list 挑 case、--items／--batch 選子集，三選一，未指定時退回
`--collection` 或既有的 `postman/collection_readonly_partial.json`）：

    python partial_verify.py --list
        列出 postman/collection_readonly.json 全部 case 的編號與名稱，
        不執行驗證

    python partial_verify.py --items 1,2
    python partial_verify.py --items locals,language
        用編號或名稱（可混用、逗號分隔）從完整 collection 挑出這次要測
        的子集——這次測 1、2，下次想測別的就換成 --items 3,4，不用手動
        改 JSON 檔案

    python partial_verify.py --batch 1/5
        把完整 collection 依原始順序切成 5 等分，跑第 1 份——例如完整
        20 個 API 分 5 次 task，每個 task 4 個，測完一個 task 抓到 bug
        就去修，修完再跑下一個 --batch N/5

    python partial_verify.py --collection postman/my_custom.json
        直接指定一份已經組好的 collection 檔案（原本的用法，仍然支援）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile

from dotenv import load_dotenv

load_dotenv()

from python_service import manager as python_service_manager
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.fixtures.db_env import DbEnvironment
from refactor_harness.verifier.comparator import GoldenVerifier

import yaml

with open("config/harness.yaml", encoding="utf-8") as f:
    _HARNESS_CONFIG = yaml.safe_load(f)

TABLES = _HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]

FULL_READONLY_COLLECTION = "postman/collection_readonly.json"
DEFAULT_PARTIAL_COLLECTION = "postman/collection_readonly_partial.json"


def _flatten_items(collection: dict) -> list[dict]:
    """postman collection 的 item 可能巢狀在資料夾（folder）裡，這裡攤平
    成單一清單，順序就是完整 collection 裡的原始順序——`--items` 的編號、
    `--batch` 的切分都以這份攤平後的順序為準。"""
    flat: list[dict] = []

    def walk(items: list[dict]) -> None:
        for it in items:
            if "item" in it:
                walk(it["item"])
            else:
                flat.append(it)

    walk(collection.get("item", []))
    return flat


def _load_full_items() -> tuple[dict, list[dict]]:
    with open(FULL_READONLY_COLLECTION, encoding="utf-8") as f:
        collection = json.load(f)
    return collection, _flatten_items(collection)


def _print_list() -> None:
    _, items = _load_full_items()
    print(f"{FULL_READONLY_COLLECTION} 共 {len(items)} 個 case：\n")
    for i, it in enumerate(items, start=1):
        req = it.get("request", {})
        method = req.get("method", "?")
        print(f"  {i:>2}. [{method}] {it.get('name')}")


def _select_by_items(spec: str) -> list[dict]:
    """spec 是逗號分隔的編號（1-based，對應 --list 印出的編號）或名稱
    （不分大小寫比對 item name），可以混用，例如 "1,3,locals"。"""
    _, items = _load_full_items()
    by_index = {i: it for i, it in enumerate(items, start=1)}
    by_name = {it.get("name", "").lower(): it for it in items}

    selected: list[dict] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit() and int(token) in by_index:
            selected.append(by_index[int(token)])
        elif token.lower() in by_name:
            selected.append(by_name[token.lower()])
        else:
            raise SystemExit(
                f"--items 裡的 {token!r} 不是有效的編號或名稱，先用 --list 確認可用清單"
            )
    return selected


def _select_by_batch(spec: str) -> list[dict]:
    """spec 格式 "N/TOTAL"（1-based），依完整 collection 原始順序切成
    TOTAL 等分（最後一份可能比較少），回傳第 N 份。"""
    try:
        n_str, total_str = spec.split("/")
        n, total = int(n_str), int(total_str)
    except ValueError:
        raise SystemExit(f"--batch 格式錯誤，應為 N/TOTAL（例如 1/5），收到 {spec!r}")
    if total < 1 or not (1 <= n <= total):
        raise SystemExit(f"--batch {spec!r} 不合法：N 必須介於 1 與 TOTAL 之間")

    _, items = _load_full_items()
    chunk_size = -(-len(items) // total)  # ceil division
    start = (n - 1) * chunk_size
    chunk = items[start : start + chunk_size]
    if not chunk:
        raise SystemExit(f"--batch {spec!r}：第 {n} 份是空的（TOTAL 切得比實際 case 數還多）")
    return chunk


def _write_temp_collection(items: list[dict]) -> str:
    full, _ = _load_full_items()
    partial = {
        "info": {**full["info"], "name": full["info"]["name"] + "-selected"},
        "item": items,
    }
    if "variable" in full:
        partial["variable"] = full["variable"]
    fd, path = tempfile.mkstemp(prefix="partial_verify_", suffix=".json", dir="postman")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(partial, f, ensure_ascii=False, indent=2)
    return path


async def run_partial_verify(collection_path: str) -> dict:
    python_project_path = os.environ["PYTHON_PROJECT_PATH"]
    python_base_url = os.environ.get("PYTHON_BASE_URL", "http://localhost:8000")
    test_dsn = os.environ.get("TEST_DB_DSN", "")

    print(f"啟動 Python 服務容器（{python_project_path}）...")
    await python_service_manager.ensure_started(python_project_path, python_base_url)

    try:
        db = DbEnvironment(test_dsn=test_dsn)
        db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

        print(f"跑 golden 驗證：{collection_path}")
        verifier = GoldenVerifier(python_base_url=python_base_url, golden_dir="fixtures/golden")
        raw = verifier.verify_raw(collection_path)

        report = HarnessReporter().build_report(raw, excluded_folders=[])
        return report
    finally:
        print("關閉 Python 服務容器...")
        await python_service_manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="列出完整 collection 的編號與名稱，不執行驗證")
    group.add_argument("--items", help="逗號分隔的編號或名稱，從完整 collection 挑子集，例如 1,2 或 locals,language")
    group.add_argument("--batch", help="N/TOTAL，把完整 collection 切成 TOTAL 等分並跑第 N 份，例如 1/5")
    group.add_argument("--collection", help="直接指定一份已經組好的 collection 檔案路徑")
    args = parser.parse_args()

    if args.list:
        _print_list()
        return

    temp_path: str | None = None
    if args.items:
        items = _select_by_items(args.items)
        print("這次挑選的 case：")
        for it in items:
            print(f"  - {it.get('name')}")
        temp_path = _write_temp_collection(items)
        collection_path = temp_path
    elif args.batch:
        items = _select_by_batch(args.batch)
        print(f"--batch {args.batch} 挑選的 case：")
        for it in items:
            print(f"  - {it.get('name')}")
        temp_path = _write_temp_collection(items)
        collection_path = temp_path
    elif args.collection:
        collection_path = args.collection
    else:
        collection_path = DEFAULT_PARTIAL_COLLECTION

    try:
        report = asyncio.run(run_partial_verify(collection_path))
    finally:
        if temp_path:
            os.remove(temp_path)

    print("\n=== 局部真實測試報告 ===")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if report["failures"]:
        print("\n失敗案例：")
        for f in report["failures"]:
            print(f"  - {f['case_id']}（{f['failure_type']}）")
    else:
        print("\n全部通過。")


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    main()
