"""
Harness DB 環境管理 stub
實作見 02b_harness_code.md
"""


class DbEnvironment:
    """
    管理測試 DB 的初始化與清理
    見 00_refactor_architecture.md 五 與 02a_harness_architecture.md
    """

    def __init__(self, test_dsn: str):
        """
        Args:
            test_dsn: 測試 DB 的連線字串（如 postgresql://.../_TEST）
                     與 .env 的 TEST_DB_DSN 同值
        """
        self.test_dsn = test_dsn

    def apply_seed(self, seed_sql_path: str, tables_to_truncate: list[str] | None = None):
        """
        Truncate 指定表，再執行 seed.sql 灌入初始資料

        TODO: 實作 psycopg2/asyncpg 連線、truncate、seed 邏輯
        見 02a/02b

        Args:
            seed_sql_path: 相對路徑，如 fixtures/seed.sql
            tables_to_truncate: 要清空的表名清單（若 None 則自動偵測）
        """
        # stub
        pass
