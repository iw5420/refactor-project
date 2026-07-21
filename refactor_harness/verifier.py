"""
Harness Verifier stub
實作見 02b_harness_code.md
"""


class GoldenVerifier:
    """
    對比 golden output 與實際 API 回應
    見 02a_harness_architecture.md
    """

    def __init__(self, python_base_url: str, golden_dir: str):
        """
        Args:
            python_base_url: Python 服務的 base URL（如 http://localhost:8000）
            golden_dir: 存放 golden output JSON 的目錄（如 fixtures/golden）
        """
        self.python_base_url = python_base_url
        self.golden_dir = golden_dir
        self.tables = []  # TODO: 從 golden 檔案掃出需要 truncate 的表名

    def verify_module(self, collection_path: str, module_filter: str) -> dict:
        """
        局部驗證：執行指定 module 對應的 API 集合，對比 golden output

        TODO: 實作呼叫 newman 執行 Postman collection 的邏輯
        見 02a/02b

        Args:
            collection_path: Postman collection 檔案路徑
            module_filter: 只驗證此 module 的 API（對應 golden/{module}/ 子目錄）

        Returns:
            {
                "module": str,
                "status": "pass" | "fail",
                "details": {...}  # fail case 的 diff 報告
            }
        """
        # stub
        return {
            "module": module_filter,
            "status": "pass",
            "details": {},
        }
