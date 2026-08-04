"""[B] Collection Agent：人工填值機制，對應 03a 三章「人工填值機制」全節。

階段一（`generate_manual_fill_templates()`）在 [A] Spec Agent 產出
`openapi.json` 後立刻執行，對每個需要動態值的 mutation endpoint 主動產生
模板，依 OpenAPI `tags` 分 controller、一個 controller 一份檔案，強制涵蓋
全部、不是「LLM 猜失敗才生成」。人工填完後才進入階段二（見
`spec_collection_agent/__init__.py`）：鏈式依賴偵測（先過濾標記 `skip`
的 endpoint）、folder 分組、套用人工填值（`value_filler.
apply_manual_fill_to_collections()`）、注入、寫出。

模板檔案**永久保留、不刪除**——鏈式依賴注入、newman 執行等後續流程萬一
發現問題，人工填過的原始資料要能找得回來；保留下來本身也是一份可稽核
的紀錄。連帶影響：`list_pending()` 不能再用「檔案存不存在」判斷 pending，
要逐筆讀模板內容依 `ManualFillEntry.is_resolved` 判斷。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# spec_collection_agent/ 相對於 orchestrator 專案根目錄的深度是 1 層
# （沿用 java_service.py 的既有慣例），用來把 file_upload 的相對路徑
# 解析成不依賴 cwd 的絕對路徑。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FillMode(str, Enum):
    FIELDS = "fields"  # body 是 object：填 values（點號路徑）
    RAW_BODY = "raw_body"  # body 不是 object（如陣列）：填 raw_body，整包覆蓋
    FILE_UPLOAD = "file_upload"  # multipart/form-data：填 file_paths


class Decision(str, Enum):
    FILL = "fill"  # 預設：等待人工填 values／raw_body／file_paths
    # 人工主動判斷這個 endpoint 是特例，不走一般重構驗證流程（例如依賴
    # OCR、語音辨識這類需要真實內容才有意義的處理）——不是「填不出值、
    # 放棄」，是編輯決定，見 03a 三章「Decision.SKIP 的語意」。
    SKIP = "skip"


@dataclass
class ManualFillEntry:
    """對應 controller 模板檔案裡 `endpoints` 陣列的單筆項目。"""

    endpoint: str
    param_schema: dict[str, Any]
    # 選填，人工自己寫的註記（例如為什麼這個 endpoint 要 skip）。取代
    # 舊版的 `error` 欄位——舊欄位記錄「LLM 為什麼填值失敗」，現在沒有
    # LLM 嘗試這件事，沒有系統自動填的失敗訊息可記錄。
    note: str | None = None
    fill_mode: FillMode = FillMode.FIELDS
    values: dict[str, Any] = field(default_factory=dict)
    raw_body: Any = None
    file_paths: dict[str, str] = field(default_factory=dict)
    decision: Decision = Decision.FILL
    # 系統寫入，人工不用填也不用清（見 record_apply_result()）。記錄上一次
    # 嘗試把這個 entry 的值套進 Postman item 時失敗的原因；`None` 代表沒
    # 試過，或上次成功。跟 `decision=skip` 是兩種不同的排除分類——`skip`
    # 是人工的編輯決定，這個欄位代表「填了但套不進去，屬於 retry」（見
    # 03a 三章「排除結果的兩層分類」「套用失敗的重填機制」）。
    last_apply_error: str | None = None

    @property
    def has_value(self) -> bool:
        """依 `fill_mode` 判斷人工是否已經提供對應的值，**不考慮**
        `last_apply_error`——單純回答「值填了沒」。跟 `is_resolved` 是
        不同的問題：一個帶著上一輪 `last_apply_error` 的 entry 可能
        `has_value=True`（值填了）但 `is_resolved=False`（上次套用失敗，
        還沒證實這次能用）。`value_filler.apply_manual_fill_to_collections()`
        用這個屬性判斷「值填了沒有」以決定要不要嘗試套用，`is_resolved`
        則用在 `list_pending()`/暫停關卡這類「這一輪算不算完成」的判斷。
        """
        match self.fill_mode:
            case FillMode.FIELDS:
                return bool(self.values)
            case FillMode.RAW_BODY:
                return self.raw_body is not None
            case FillMode.FILE_UPLOAD:
                return bool(self.file_paths)
        return False

    @property
    def is_resolved(self) -> bool:
        """人工是否已經給出可用的答案（填值或明確跳過）。`decision=skip`
        一律視為已解決；`decision=fill` 時除了值已填（`has_value`），還要
        上一次套用沒有失敗（`last_apply_error is None`）才算——套用失敗
        會讓已填值的 entry 變回未解決，跟「尚未填值」是同一種 retry 狀態。
        """
        if self.decision == Decision.SKIP:
            return True
        return self.has_value and self.last_apply_error is None


def _sanitize_controller(controller: str) -> str:
    """把 controller tag（如 "exam-controller"）轉成安全的檔名。"""
    return re.sub(r"[^A-Za-z0-9]+", "_", controller).strip("_")


def _controller_file_path(controller: str, manual_fill_dir: Path) -> Path:
    return manual_fill_dir / f"{_sanitize_controller(controller)}.json"


def _entry_to_dict(entry: ManualFillEntry) -> dict[str, Any]:
    return {
        "endpoint": entry.endpoint,
        "param_schema": entry.param_schema,
        "note": entry.note,
        "fill_mode": entry.fill_mode.value,
        "values": entry.values,
        "raw_body": entry.raw_body,
        "file_paths": entry.file_paths,
        "decision": entry.decision.value,
        "last_apply_error": entry.last_apply_error,
    }


def _entry_from_dict(raw: dict[str, Any]) -> ManualFillEntry:
    return ManualFillEntry(
        endpoint=raw["endpoint"],
        param_schema=raw.get("param_schema", {}),
        note=raw.get("note"),
        fill_mode=FillMode(raw.get("fill_mode", "fields")),
        values=raw.get("values") or {},
        raw_body=raw.get("raw_body"),
        file_paths=raw.get("file_paths") or {},
        decision=Decision(raw.get("decision", "fill")),
        last_apply_error=raw.get("last_apply_error"),
    )


def _default_fill_mode(param_schema: dict[str, Any]) -> FillMode:
    """依 body 實際形狀決定模板產生時的預設 fill_mode（見 03a 三章
    「ManualFillEntry 欄位」）：`multipart/form-data` → `file_upload`；
    body 根層是 array → `raw_body`；其餘（object 或沒有 body、只有
    path/query 參數）→ `fields`。
    """
    request_body = param_schema.get("requestBody")
    if not isinstance(request_body, dict):
        return FillMode.FIELDS
    content = request_body.get("content")
    if not isinstance(content, dict):
        return FillMode.FIELDS
    if "multipart/form-data" in content:
        return FillMode.FILE_UPLOAD
    for media_schema in content.values():
        schema = media_schema.get("schema") if isinstance(media_schema, dict) else None
        if isinstance(schema, dict) and schema.get("type") == "array":
            return FillMode.RAW_BODY
    return FillMode.FIELDS


def generate_manual_fill_templates(
    *, openapi_spec: dict[str, Any], manual_fill_dir: Path
) -> list[str]:
    """階段一：對每個需要填值的 mutation endpoint，依 controller 產生／
    更新人工填值模板（見 03a 三章「人工填值機制」階段一）。

    **已存在的 endpoint 項目不覆寫**，只新增這次才出現的——`openapi.json`
    有變動、重跑這一步時是安全的，不會動到既有、已經處理過的部分（不管
    已解決還是尚未解決）。回傳這次新增的 endpoint 清單，給呼叫端知道有
    沒有新東西要填。
    """
    # 延遲 import 避免模組載入順序的循環依賴（chain_dependency_detect.py
    # 沒有 import 這個模組，value_filler.py 也沒有，方向是單向的，但延遲
    # import 讓相依關係在讀程式碼時更明確：這兩個函式只在產生模板時才
    # 需要）。
    from spec_collection_agent.chain_dependency_detect import group_operations_by_tag
    from spec_collection_agent.types import MUTATION_METHODS
    from spec_collection_agent.value_filler import _operation_param_schema

    manual_fill_dir.mkdir(parents=True, exist_ok=True)
    groups = group_operations_by_tag(openapi_spec)

    added: list[str] = []
    for controller, operations in groups.items():
        path = _controller_file_path(controller, manual_fill_dir)
        entries: dict[str, ManualFillEntry] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            for raw_entry in raw.get("endpoints", []):
                entry = _entry_from_dict(raw_entry)
                entries[entry.endpoint] = entry

        for op_path, method, _operation in operations:
            if method not in MUTATION_METHODS:
                continue
            param_schema = _operation_param_schema(openapi_spec, op_path, method)
            if param_schema is None:
                continue  # 沒有 path/query/body 參數，不需要人工填值

            endpoint = f"{method} {op_path}"
            if endpoint in entries:
                continue  # 已存在（不管解不解決），不覆寫

            entries[endpoint] = ManualFillEntry(
                endpoint=endpoint,
                param_schema=param_schema,
                fill_mode=_default_fill_mode(param_schema),
            )
            added.append(endpoint)

        if not entries:
            continue  # 這個 controller 沒有任何需要填值的 endpoint，不產生空檔案

        with path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "controller": controller,
                    "endpoints": [_entry_to_dict(e) for e in entries.values()],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    return added


def read_entry(endpoint: str, manual_fill_dir: Path) -> ManualFillEntry | None:
    """跨所有 controller 檔案找這個 endpoint 的模板項目。呼叫端不一定知道
    這個 endpoint 屬於哪個 controller，掃描檔案數量少（依 controller 分組，
    通常只有幾個到十幾個檔案），直接掃描比額外維護一份
    endpoint→controller 索引簡單。
    """
    if not manual_fill_dir.exists():
        return None
    for path in manual_fill_dir.glob("*.json"):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for raw_entry in raw.get("endpoints", []):
            if raw_entry.get("endpoint") == endpoint:
                return _entry_from_dict(raw_entry)
    return None


def list_pending(manual_fill_dir: Path) -> list[str]:
    """回傳尚未解決（既沒填值、也沒標記 skip）的 endpoint 清單，見 01 五
    章 conditional edge 判斷依據。模板不刪除，不能再用「檔案存不存在」
    判斷，逐筆讀模板內容依 `ManualFillEntry.is_resolved` 判斷。
    """
    if not manual_fill_dir.exists():
        return []
    pending: list[str] = []
    for path in sorted(manual_fill_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for raw_entry in raw.get("endpoints", []):
            entry = _entry_from_dict(raw_entry)
            if not entry.is_resolved:
                pending.append(entry.endpoint)
    return pending


def list_skipped(manual_fill_dir: Path) -> list[str]:
    """回傳已標記 `Decision.SKIP` 的 endpoint 清單。呼叫端（`__init__.py`
    階段二）把這份清單跟 `list_pending()` 合併，一起在跑鏈式依賴偵測之前
    先過濾掉（見 03a 三章「輸入前先過濾尚未就緒的 endpoint」）——skip 與
    pending 是兩種不同的排除分類（見「排除結果的兩層分類」），各自用
    對應的函式回傳，不在這裡合併，合併留給呼叫端依用途決定。
    """
    if not manual_fill_dir.exists():
        return []
    skipped: list[str] = []
    for path in sorted(manual_fill_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for raw_entry in raw.get("endpoints", []):
            entry = _entry_from_dict(raw_entry)
            if entry.decision == Decision.SKIP:
                skipped.append(entry.endpoint)
    return skipped


def record_apply_result(
    endpoint: str, manual_fill_dir: Path, *, error: str | None
) -> None:
    """套用人工填值後回寫這次嘗試的結果到對應模板檔案的
    `last_apply_error`——失敗記錄原因，成功則清空（見 03a 三章「套用失敗
    的重填機制」）。只在值真的改變時才寫檔，避免每次套用成功都重寫一份
    沒有變化的模板檔案。找不到對應 entry（理論上不該發生，呼叫端一定是
    先 `read_entry()` 讀到了才會走到套用這一步）時靜默略過，不拋例外。
    """
    if not manual_fill_dir.exists():
        return
    for path in manual_fill_dir.glob("*.json"):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        target = next(
            (e for e in raw.get("endpoints", []) if e.get("endpoint") == endpoint),
            None,
        )
        if target is None:
            continue
        if target.get("last_apply_error") != error:
            target["last_apply_error"] = error
            with path.open("w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        return


def apply_manual_fill(item: dict, entry: ManualFillEntry) -> None:
    """把人工補值套進 Postman item。

    url variable/query 跟 `fill_mode` 無關，一律先套用 `entry.values`
    裡對得上的部分（`_apply_url_values()`）——`multipart/form-data` 或
    `raw_body` 的 endpoint 常常同時帶著 query 參數（例如
    `POST /api/file/image` 的 `kind`/`randomId`/`side` 是 query 參數，
    `file` 才是 multipart body），這幾個參數不該因為 body 走
    `file_upload`／`raw_body` 模式就沒有管道被填、只能拿到
    `openapi-to-postmanv2` 產生的預設佔位值——那正是 module docstring
    講的 False Pass 風險本尊（見 03a 三章「檔案上傳」更正）。

    body 本身才依 `fill_mode` 分派——三種模式處理的資料形狀完全不同
    （逐欄位／整包覆蓋／檔案路徑），match-case 讓分支邊界更明確，之後要
    加第四種模式也只需要多一個 case。`FillMode.FIELDS` 內部會重複呼叫一次
    `_apply_url_values()`（`_apply_values_to_item()` 自己也需要用它決定
    哪些 key 剩下要寫進 body），是有意的重複、不是遺漏——兩次呼叫都是
    冪等操作，成本可忽略。
    """
    from spec_collection_agent.value_filler import _apply_url_values, _apply_values_to_item

    _apply_url_values(item, entry.values)

    match entry.fill_mode:
        case FillMode.FIELDS:
            _apply_values_to_item(item, entry.values)
        case FillMode.RAW_BODY:
            _apply_raw_body(item, entry.raw_body)
        case FillMode.FILE_UPLOAD:
            _apply_file_upload(item, entry.file_paths)
        case _:
            raise ValueError(f"不支援的 fill_mode: {entry.fill_mode!r}")


def _apply_raw_body(item: dict, raw_body: Any) -> None:
    """整包覆蓋 body.raw，供 body 根層級不是 object（如陣列）的 endpoint
    使用。不引入 Pydantic 之類的 schema 驗證套件——人工填的值是當下手動
    確認過的資料、只使用一次，不是長期需要防禦的外部輸入，過度驗證只是
    徒增依賴（跟 01 三章「用 TypedDict 而非 Pydantic」判斷一致）。
    `json.dumps()` 失敗（如值本身不可序列化）會自然拋 `TypeError`，交由
    呼叫端（`value_filler.apply_manual_fill_to_collections()`）當成套用
    失敗處理。
    """
    body = item.setdefault("request", {}).setdefault("body", {})
    body["mode"] = "raw"
    body["raw"] = json.dumps(raw_body, ensure_ascii=False, indent=2)


def _apply_file_upload(item: dict, file_paths: dict[str, str]) -> None:
    """把人工指定的檔案路徑套進 formdata 的 file 欄位（見 03a 三章
    「檔案上傳」）。`file_paths` 是「formdata 欄位名稱 → 相對於專案根
    目錄的檔案路徑」的對應。找不到對應 formdata 項目、或專案根目錄下
    找不到指定的檔案，都直接拋 `ValueError`——不要讓一個指向不存在檔案
    的 src 悄悄進 Collection，newman 執行時才用「檔案讀不到」的方式
    失敗，現場會比在這裡直接拋錯更難追。
    """
    body = item.get("request", {}).get("body")
    if not isinstance(body, dict) or body.get("mode") != "formdata":
        raise ValueError("item 的 body 不是 formdata 模式，無法套用 file_upload")

    entries_by_key = {
        entry["key"]: entry
        for entry in body.get("formdata", [])
        if entry.get("type") == "file"
    }
    for key, path_str in file_paths.items():
        formdata_entry = entries_by_key.get(key)
        if formdata_entry is None:
            raise ValueError(f"formdata 裡找不到 file 型別的欄位 {key!r}")
        file_path = _PROJECT_ROOT / path_str
        if not file_path.is_file():
            raise ValueError(f"file_paths[{key!r}] 指向的檔案不存在: {path_str}")
        formdata_entry["src"] = path_str
