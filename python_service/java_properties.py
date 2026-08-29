"""讀取 Java 端 `application-{profile}.properties`，解析 `@Value` 注入用的
實際屬性值，對應 `docs/09b_bug_trace.md` #46「@Value 屬性注入機制只設計了
「怎麼命名／怎麼讀」，沒有設計「值從哪裡來」」。

`design_agent/global_infra.py::render_config_py()`（③）已經決定「用
環境變數」這個機制、也已經算出每個 `@Value` 對應的環境變數常數名稱
（`PythonStructure.config_env_vars`，見 `graph/state.py`）——這裡負責
容器啟動前這一段：**這些常數該填什麼值**。

**profile 選擇不需要新增決策點**：Java 端 `application.properties`
（base）本身就用 Spring Boot 標準慣例宣告了 `spring.profiles.active`
（如 `spring.profiles.active=macuhau`）——這是 Java 專案自己既有的設定，
機械讀取即可，不需要 pipeline 另外決定「這次跑哪個 profile」（呼應 00
二章「能用程式判斷的，就不要交給 LLM」，這裡連 LLM 都不需要，純粹是
既有事實的機械讀取）。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Maven／Spring Boot 標準專案佈局，不是這個 pipeline 自訂的慣例——
# lang-exam-api-refactor 的 application*.properties 都在這個固定位置。
_RESOURCES_DIR = "src/main/resources"
_BASE_PROPERTIES_FILE = "application.properties"
_PROFILE_PROPERTIES_TEMPLATE = "application-{profile}.properties"

_PROFILES_ACTIVE_KEY = "spring.profiles.active"


def _parse_properties_file(path: Path) -> dict[str, str]:
    """最小 Java `.properties` 格式解析器：跳過空行、`#`／`!` 開頭的
    註解行，以第一個 `=` 或 `:` 分隔 key/value，解碼 `\\uXXXX` unicode
    escape（Java `.properties` 標準格式對非 ASCII 字元的既有寫法，見
    `application-macuhau.properties` 的 `language.displayName`）。不引入
    第三方套件——這裡要解析的格式單純（純 key=value，沒有續行、沒有
    巢狀結構），自己實作比引入依賴划算，也避免這個 Orchestrator 專案
    多一條跟 Java 生態系工具鏈耦合的依賴。

    檔案不存在時回傳空字典，不拋例外——呼叫端（`resolve_config_env_
    values()`）本來就要能容忍「這個 profile 沒有專屬檔案」的情況（例如
    base `application.properties` 已經涵蓋所有需要的 key，沒有 profile
    專屬覆寫）。
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        for sep in ("=", ":"):
            if sep in stripped:
                key, _, value = stripped.partition(sep)
                result[key.strip()] = value.strip().encode().decode("unicode_escape")
                break
    return result


def resolve_active_profile(java_project_path: str) -> str | None:
    """讀 base `application.properties` 裡的 `spring.profiles.active`。
    找不到檔案或找不到這個 key 都回傳 `None`（記警告，不拋例外）——沒有
    宣告 active profile 的 Java 專案不一定用得到這個機制，讓呼叫端決定
    要不要因此跳過 profile 專屬檔案，不在這裡就中止。
    """
    base_path = Path(java_project_path, _RESOURCES_DIR, _BASE_PROPERTIES_FILE)
    base_props = _parse_properties_file(base_path)
    profile = base_props.get(_PROFILES_ACTIVE_KEY)
    if profile is None:
        logger.warning(
            "%s 找不到 %s，無法判斷要疊加哪個 application-{profile}.properties",
            base_path, _PROFILES_ACTIVE_KEY,
        )
    return profile


def resolve_config_env_values(
    java_project_path: str, config_env_vars: list[dict[str, str]]
) -> dict[str, str]:
    """依 `config_env_vars`（③ 輸出，`PythonStructure.config_env_vars`，
    每筆 `{"property_key": ..., "constant_name": ...}`）逐一查出 Java 端
    實際屬性值，回傳 `{constant_name: value}` 供容器啟動時當額外 `-e`
    環境變數注入。

    **合併順序：profile 專屬蓋過 base**，比照 Spring Boot 自己的既有合併
    語意——`application-{profile}.properties` 的值優先，`application.
    properties`（base）補上 profile 沒有覆寫的 key。

    `config_env_vars` 為空、或找不到 active profile（`resolve_active_
    profile()` 回傳 `None`）時，只用 base properties 查找——base 檔案
    本身也可能直接宣告某些 key（不是每個 `@Value` 屬性都放在 profile
    專屬檔案），不因為沒有 profile 就整個放棄。

    查不到的 `property_key` 記警告、略過（不注入這個環境變數，不用猜的
    值掩蓋落差）——容器啟動後 `os.environ[...]` 讀取失敗時的 `KeyError`
    訊息本身就足夠明確，指向這個常數名稱，比在這裡塞一個錯誤的假值
    更容易定位問題根源。
    """
    if not config_env_vars:
        return {}

    base_path = Path(java_project_path, _RESOURCES_DIR, _BASE_PROPERTIES_FILE)
    merged = _parse_properties_file(base_path)

    profile = resolve_active_profile(java_project_path)
    if profile is not None:
        profile_path = Path(
            java_project_path, _RESOURCES_DIR, _PROFILE_PROPERTIES_TEMPLATE.format(profile=profile)
        )
        merged.update(_parse_properties_file(profile_path))

    resolved: dict[str, str] = {}
    for entry in config_env_vars:
        property_key = entry["property_key"]
        constant_name = entry["constant_name"]
        value = merged.get(property_key)
        if value is None:
            logger.warning(
                "Java 端 application.properties／application-%s.properties 都找不到 "
                "property key %r（對應環境變數 %s），容器啟動時不會注入這個環境變數，"
                "若 app/core/config.py 讀取它會直接 KeyError",
                profile, property_key, constant_name,
            )
            continue
        resolved[constant_name] = value
    return resolved
