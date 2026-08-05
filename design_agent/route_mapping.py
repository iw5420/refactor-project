# design_agent/route_mapping.py
"""③ 架構設計 Agent：route_to_file_mapping 機械合併，對應 05a 八章
全節。完全是程式邏輯，不需要 LLM——所有需要的資訊（`api_to_python_
target` 的 endpoint↔module 對應、每個 module 的 `interfaces` 檔案集合）
在六章都已經產出完畢。
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


def build_route_to_file_mapping(
    api_to_python_target: list[ApiMapping],
    interfaces: list[InterfaceSpec],
    modules_with_schema_file: set[str],
) -> dict[str, list[str]]:
    """對應 05a 八章「`related_files` 的組成」：一個 `ApiMapping` 項目的
    `related_files` = 該項目 `module` 底下所有 `interfaces` 的
    `file_path`（去重），加上（若該 module 有對應的
    `schemas/{module}.py`——即這個 module 產出過任何具名 schema）該
    schema 檔案路徑，供 Debug Agent 除錯參考（見 05a 八章：`missing_
    fields`／`type_mismatch` 的 debug_hint 明確指向「檢查 Pydantic
    schema」，缺了這個檔案路徑會讓 Debug Agent 少一個關鍵線索）。

    這裡的 `related_files` 給整個 module 的檔案集合，不嘗試精算「這次
    呼叫實際上只會執行到哪幾個函式」——後者需要真正的呼叫鏈分析，成本
    遠高於效益，也符合 04a 反覆強調的「多連、少排除」保守精神（見 05a
    八章「設計原則延續」）。

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

    mapping: dict[str, list[str]] = {}
    for api in api_to_python_target:
        key = normalize_path_key(api["http_method"], api["endpoint"])
        related = set(files_by_module.get(api["module"], set()))
        if api["module"] in modules_with_schema_file:
            related.add(schema_file_path(api["module"]))
        mapping[key] = sorted(related)
    return mapping


def write_route_to_file_mapping(
    route_to_file_mapping: dict[str, list[str]], config_path: str = "config/harness.yaml"
) -> None:
    """對應 05a 九章、00 八章「`route_to_file_mapping` 產出後直接寫入
    `config/harness.yaml`，不需人工填寫」——這裡是實際落地檔案 I/O 的
    地方，不只是回傳給 State 就結束（`config/harness.yaml` 的
    `RouteMapper` 是純讀檔案的類別，不吃 LangGraph State，見 02a 十一章，
    Harness 驗證階段能讀到這份 mapping 的前提就是這個檔案真的被寫到
    磁碟上）。跟 `spec_node.py`／`collection_node.py` 呼叫的
    `run_spec_agent()`／`run_collection_agent()` 一樣，檔案 I/O 副作用
    放在 Agent 自己的執行過程裡完成，不留到流程末端另外用一個
    Orchestrator 步驟去沖刷，這是這個專案既有的一貫做法。

    **只覆寫 `route_to_file_mapping` 這個 key**，其餘段落（
    `databases`／`services`／`collections`／`diff_rules`，這些是人工在
    00 五章、02a 設定好的環境設定，不是③的產出）讀進來後原樣保留、寫
    回去——`config/harness.yaml` 不是③獨佔的檔案，是跟 Harness 共用的
    設定檔，見 `config/harness.yaml` 檔案內既有的註解「此區段由 Agent
    ③（架構設計 Agent）自動產生並寫入，不應手動維護」，只針對這一段。

    **已知限制**：用 PyYAML 的 `safe_load`／`safe_dump` 做「讀取＋覆寫＋
    寫回」，不是保留註解的 round-trip parser（如 `ruamel.yaml`）——
    `config/harness.yaml` 裡原本給人看的註解（如上面提到的那行警語）在
    第一次被③寫入後會消失，其餘機器可讀的 key/value 不受影響，只有
    註解會不見。這是接受的取捨，不是遺漏：專案目前唯一用到的 YAML
    函式庫是 PyYAML（`refactor_harness/core/route_mapper.py` 已經在用），
    為了保留註解另外引入一個新函式庫，成本高於這個取捨的代價。
    """
    path = Path(config_path)
    config: dict = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    config["route_to_file_mapping"] = route_to_file_mapping
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
