# design_agent/route_mapping.py
"""③ 架構設計 Agent：route_to_file_mapping／route_to_module_mapping 機械
合併，對應 05a 八章全節（設計理由詳見該章，這裡不重複）。完全是程式
邏輯，不需要 LLM——所有需要的資訊（`api_to_python_target` 的
endpoint↔module 對應、每個 module 的 `interfaces` 檔案集合）在六章都
已經產出完畢。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from design_agent.layout import schema_file_path
from graph.state import ApiMapping, InterfaceSpec

_PATH_PARAM_RE = re.compile(r"\{[^{}]+\}")


def normalize_path_key(http_method: str, endpoint: str) -> str:
    """對應 05a 八章「Key 格式」：**必須跟 02a `RouteMapper.
    normalize_path_key()` 的正規化結果完全一致**，否則 Harness 的比對
    會全部 miss。`endpoint` 是 openapi 的 path 樣板（如
    `/api/v1/users/{userId}`），逐字元處理，不是「切段、丟掉空字串、
    再重組」：

    1. 去除開頭 `/`（只去開頭這一個，不動其他任何 `/`）
    2. 所有（剩下的）`/` 換成 `_`
    3. 所有 `{paramName}` 樣板，不論原始參數名稱是什麼，一律換成字面
       `{id}`——因為 `RouteMapper.normalize_path_key()` 是在**實際 URL**
       上把「純數字/UUID」換成 `{id}`，並不知道原始參數名稱叫什麼，
       兩邊要能精確匹配，這裡也必須捨棄參數名稱、統一用 `{id}`

    ```
    /api/v1/users/{userId} + GET  → GET_api_v1_users_{id}
    /api/v1/users/{userId}/profiles + GET → GET_api_v1_users_{id}_profiles
    ```

    **刻意不「切段、過濾空字串、重新 join」**：那種寫法會把結尾多帶
    一個 `/` 的路徑（如 `/api/v1/users/`）跟不帶結尾 `/` 的版本
    （`/api/v1/users`）normalize 成同一個 key（都是 `..._users`）。
    但 `RouteMapper.normalize_path_key()`（`refactor_harness/core/
    route_mapper.py`）拿到的 `url_parts` 是 Postman `request.url.path`
    這個**已經切好的陣列**，它的邏輯只是逐段判斷數字/UUID、原樣
    `"_".join(...)`，不會主動丟掉空字串——若 Postman 對一個結尾帶 `/`
    的 URL 產出帶結尾空字串的 `path` 陣列，`RouteMapper` 那邊 join 出來
    的 key 會帶著結尾底線（如 `GET_api_v1_users_`），若這裡先把它
    filter 掉會兩邊對不上。因此這裡改成「只動開頭那一個 `/`、其餘
    `/` 逐一換成 `_`」的字面規則，結尾/中間是否有多餘 `/` 兩邊都會
    留下同樣的痕跡，才能保證跟 `RouteMapper` 的正規化結果位元對位元
    一致——不是靠事後幫兩邊的 key 都做 trim 去湊出一致（trim 只在兩邊
    都真的一致地被 trim 過才安全，這裡沒有把握 `RouteMapper` 那側也
    會 trim，貿然單邊 trim 反而製造新的不一致）。
    """
    path = endpoint.removeprefix("/")
    path = path.replace("/", "_")
    path = _PATH_PARAM_RE.sub("{id}", path)
    return f"{http_method.upper()}_{path}"


def build_route_to_module_mapping(api_to_python_target: list[ApiMapping]) -> dict[str, str]:
    """對應 docs/09b_bug_trace.md #64：`route_to_module_mapping` 的值就是
    `ApiMapping.module` 本尊，不是新的判斷（見 `build_route_mappings()`
    docstring），只需要 ①（parse）的輸出就能算出來，不依賴 ③ 才有的
    `interfaces`／`modules_with_schema_file`（那兩者只有 `route_to_file_
    mapping` 那半才需要，見 `build_route_mappings()` 內部組裝）。

    獨立成這個函式，讓 `record_tests`／`run_tests` 這兩個 node 可以在
    ① 完成、③ 還沒跑完（甚至根本不會跑，如 `run_tests` 在 debug↔implement
    重試迴圈裡重複呼叫）時，直接用 `state["api_to_python_target"]` 就地
    算出當下這次 run 真正的 module 對照，不用透過 `config/harness.yaml`
    這個由 ③ 寫入、且跟 ②（`record_tests`）是平行分支、寫入時機不保證
    早於 ② 讀取的中介檔案——`record_tests`／`design` 平行觸發（05a 十一
    章），先前 ② 用 `RouteMapper` 讀 `config/harness.yaml` 拿到的可能是
    上一輪殘留的舊版（這輪 ③ 還沒寫完），而 `run_tests` 稍後讀到的是
    這輪 ③ 已經寫完的新版——只要①這輪的模組分類跟上一輪不同，兩個 node
    對同一個 route 解出不同 module，golden 寫錯資料夾、永遠讀不到。
    """
    return {
        normalize_path_key(api["http_method"], api["endpoint"]): api["module"]
        for api in api_to_python_target
    }


def build_route_mappings(
    api_to_python_target: list[ApiMapping],
    interfaces: list[InterfaceSpec],
    modules_with_schema_file: set[str],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """對應 05a 八章全節（`related_files` 的組成、route_to_module_mapping
    的存在理由，詳見該章，這裡不重複）。回傳
    `(route_to_file_mapping, route_to_module_mapping)`——`route_to_module_
    mapping` 直接委派給 `build_route_to_module_mapping()`（同一份邏輯，
    不重複維護兩份），這裡只另外組 `route_to_file_mapping`。

    **`schemas/{module}.py` 是否存在的判定，必須用 `modules_with_schema_
    file` 精確比對，不能用「這個 module 有沒有 router 檔案」猜測**：同一
    個 module 的 API 邊界方法可能全部只用 inline schema（沒有具名
    `$ref`，見 `type_mapping.schema_name_for()`），這種情況下即使有
    router 檔案，`design.py` 也不會真的產出 `schemas/{module}.py`（見
    `design_all_modules()` 的 `modules_with_schema_file` 集合，只在
    `ModuleDesignResult.directory_tree_fragment` 非空時才會收錄這個
    module）。若這裡改用「有 router 檔案就假設有 schema 檔案」的猜測，
    會讓 `related_files` 指向一個 directory_tree 裡實際上不存在的「幽靈
    檔案」，Debug Agent 跟著這個路徑去讀檔會撲空。
    """
    files_by_module: dict[str, set[str]] = {}
    for iface in interfaces:
        module = _module_of(iface["file_path"])
        files_by_module.setdefault(module, set()).add(iface["file_path"])

    module_mapping = build_route_to_module_mapping(api_to_python_target)

    file_mapping: dict[str, list[str]] = {}
    for api in api_to_python_target:
        key = normalize_path_key(api["http_method"], api["endpoint"])
        related = set(files_by_module.get(api["module"], set()))
        if api["module"] in modules_with_schema_file:
            related.add(schema_file_path(api["module"]))
        file_mapping[key] = sorted(related)
    return file_mapping, module_mapping


def write_route_mappings(
    route_to_file_mapping: dict[str, list[str]],
    route_to_module_mapping: dict[str, str],
    config_path: str = "config/harness.yaml",
) -> None:
    """對應 05a 九章、00 八章「`route_to_file_mapping` 產出後直接寫入
    `config/harness.yaml`，不需人工填寫」，以及 05a 八章
    `route_to_module_mapping` 的落地——這裡是實際落地檔案 I/O 的地方，
    不只是回傳給 State 就結束（`RouteMapper` 是純讀檔案的類別，不吃
    LangGraph State，見 02a 十一章）。跟 `spec_node.py`／
    `collection_node.py` 呼叫的 `run_spec_agent()`／
    `run_collection_agent()` 一樣，檔案 I/O 副作用放在 Agent 自己的執行
    過程裡完成，不留到流程末端另外用一個 Orchestrator 步驟去沖刷。

    **兩個 key 必須在同一次讀寫回合裡一起覆寫**：`safe_load`／
    `safe_dump` 是整檔 round-trip，分兩次呼叫各自「讀取＋覆寫單一 key
    ＋寫回」會讓後一次寫回的整份 config 蓋掉前一次剛寫入的那個 key。
    其餘段落（`databases`／`services`／`collections`／`diff_rules`，這些
    是人工在 00 五章、02a 設定好的環境設定，不是③的產出）讀進來後原樣
    保留、寫回去。

    **已知限制**：用 PyYAML 的 `safe_load`／`safe_dump` 做「讀取＋覆寫＋
    寫回」，不是保留註解的 round-trip parser（如 `ruamel.yaml`）——
    `config/harness.yaml` 裡原本給人看的註解在第一次被③寫入後會消失，
    其餘機器可讀的 key/value 不受影響，只有註解會不見。這是接受的取捨，
    不是遺漏：專案目前唯一用到的 YAML 函式庫是 PyYAML
    （`refactor_harness/core/route_mapper.py` 已經在用），為了保留註解
    另外引入一個新函式庫，成本高於這個取捨的代價。
    """
    path = Path(config_path)
    config: dict = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    config["route_to_file_mapping"] = route_to_file_mapping
    config["route_to_module_mapping"] = route_to_module_mapping
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _module_of(file_path: str) -> str:
    """從 `InterfaceSpec.file_path`（如 `app/services/user_service.py`）
    反推 module 名稱——`layout.file_path_for_layer()` 的逆運算：檔名固定
    是 `{module}_{layer_singular}.py`，`layer_singular` 只有三種
    （router／service／repository，見 05a 三章「檔名規則」），去掉這個
    後綴即為 module 名稱。
    """
    stem = file_path.rsplit("/", 1)[-1].removesuffix(".py")
    for suffix in ("_router", "_service", "_repository"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem  # 不應該發生（見 05a 三章檔名規則），保底原樣回傳
