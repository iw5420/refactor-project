import subprocess
import psycopg2


class DbEnvironment:
    def __init__(self, test_dsn: str):
        # 永遠只連 test_dsn（如 MOC_MATSUEXAM_TEST），絕不連 production DB（如 MOC_MATSUEXAM）
        self.test_dsn = test_dsn

    def apply_seed(self, seed_file: str, tables_to_truncate: list[str]):
        """
        每次測試前執行：
        1. 清除測試 DB（test_dsn）的指定資料表（不影響 production DB）
        2. 重新注入 seed data

        tables_to_truncate 從 config/harness.yaml 讀入，不寫死在程式碼裡。

        TRUNCATE 與 seed 注入包在同一個 psycopg2 交易裡——psycopg2 走 simple
        query protocol，本來就支援分號分隔的多條語句。seed 失敗時整體
        rollback，不會留下「已清空但沒灌資料」的中間態。

        ⚠️ 限制：seed.sql 必須是標準 SQL（INSERT / UPDATE / ...），
        不能包含 psql meta-command 或 COPY FROM stdin——psycopg2 無法執行這些。
        """
        with psycopg2.connect(self.test_dsn) as conn:
            with conn.cursor() as cur:
                # 1. TRUNCATE：單一指令，CASCADE 自動處理外鍵順序，避免多次鎖表
                tables_sql = ", ".join(tables_to_truncate)
                cur.execute(f"TRUNCATE TABLE {tables_sql} RESTART IDENTITY CASCADE")

                # 2. seed 注入：與 TRUNCATE 同一交易，失敗時一起 rollback
                with open(seed_file, "r", encoding="utf-8") as f:
                    seed_sql = f.read()
                cur.execute(seed_sql)
            conn.commit()

    def sync_schema(self, source_dsn: str):
        """
        當 production DB（如 MOC_MATSUEXAM）的 schema 有變動時，同步到測試 DB（test_dsn）。
        僅適用於 Flyway/Liquibase／手動 migration 的情況；若 Java 用 Hibernate/JPA
        `ddl-auto`，schema 由 [A] Spec Agent 啟動 Java 服務時自動建立，不需呼叫這個方法。
        在每次 migration 後執行一次即可。
        """
        dump = subprocess.run(
            ["pg_dump", "--schema-only", source_dsn],
            capture_output=True, text=True
        )
        if dump.returncode != 0:
            raise RuntimeError(
                f"pg_dump 失敗，schema 同步中止\n"
                f"stderr: {dump.stderr[:500]}"
            )

        psql = subprocess.run(
            ["psql", self.test_dsn],
            input=dump.stdout, capture_output=True, text=True
        )
        if psql.returncode != 0:
            raise RuntimeError(
                f"psql schema 匯入失敗\n"
                f"stderr: {psql.stderr[:500]}"
            )
        return True
