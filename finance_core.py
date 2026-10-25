from __future__ import annotations

import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


SCHEMA = r"""
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS app_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN (
        'UNALLOCATED','BUDGET','SAVINGS','INCOME_CLEARING','EXPENSE_CLEARING'
    )),
    is_asset INTEGER NOT NULL CHECK(is_asset IN (0,1)),
    balance_cents INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    archived_at TEXT,
    CHECK (
      (kind IN ('UNALLOCATED','BUDGET','SAVINGS') AND is_asset=1) OR
      (kind IN ('INCOME_CLEARING','EXPENSE_CLEARING') AND is_asset=0)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_unallocated
ON accounts(kind) WHERE kind='UNALLOCATED';

CREATE TABLE IF NOT EXISTS budget_cycles (
    id TEXT PRIMARY KEY,
    wallet_id TEXT NOT NULL,
    initial_amount_cents INTEGER NOT NULL CHECK(initial_amount_cents>0),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
      CHECK(status IN ('ACTIVE','FINISHED','CANCELLED')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(wallet_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CHECK(date(end_date)>date(start_date))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_cycle
ON budget_cycles(wallet_id) WHERE status='ACTIVE';

CREATE TABLE IF NOT EXISTS savings_goals (
    wallet_id TEXT PRIMARY KEY,
    target_cents INTEGER NOT NULL CHECK(target_cents>0),
    target_date TEXT,
    withdrawal_locked INTEGER NOT NULL DEFAULT 1 CHECK(withdrawal_locked IN (0,1)),
    FOREIGN KEY(wallet_id) REFERENCES accounts(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN (
      'INCOME','EXPENSE','ALLOCATION','TRANSFER','SAVINGS_WITHDRAWAL','REVERSAL'
    )),
    status TEXT NOT NULL DEFAULT 'DRAFT'
      CHECK(status IN ('DRAFT','POSTED','REVERSED')),
    effective_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    note TEXT,
    reverses_tx_id TEXT UNIQUE,
    replaces_tx_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(reverses_tx_id) REFERENCES ledger_transactions(id) ON DELETE RESTRICT,
    FOREIGN KEY(replaces_tx_id) REFERENCES ledger_transactions(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents<>0),
    line_role TEXT NOT NULL CHECK(line_role IN (
      'SOURCE','DESTINATION','INCOME','EXPENSE','REVERSAL'
    )),
    FOREIGN KEY(transaction_id) REFERENCES ledger_transactions(id) ON DELETE RESTRICT,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    UNIQUE(transaction_id, account_id)
);

CREATE INDEX IF NOT EXISTS ix_entries_account ON ledger_entries(account_id);
CREATE INDEX IF NOT EXISTS ix_transactions_date
ON ledger_transactions(local_date,effective_at);

CREATE TABLE IF NOT EXISTS budget_daily_state (
    cycle_id TEXT NOT NULL,
    budget_date TEXT NOT NULL,
    opening_future_pool_cents INTEGER NOT NULL CHECK(opening_future_pool_cents>=0),
    base_budget_cents INTEGER NOT NULL CHECK(base_budget_cents>=0),
    opening_rollover_cents INTEGER NOT NULL CHECK(opening_rollover_cents>=0),
    expense_cents INTEGER NOT NULL DEFAULT 0 CHECK(expense_cents>=0),
    transfer_in_cents INTEGER NOT NULL DEFAULT 0 CHECK(transfer_in_cents>=0),
    transfer_out_cents INTEGER NOT NULL DEFAULT 0 CHECK(transfer_out_cents>=0),
    overrun_cents INTEGER NOT NULL DEFAULT 0 CHECK(overrun_cents>=0),
    closing_future_pool_cents INTEGER NOT NULL CHECK(closing_future_pool_cents>=0),
    closing_rollover_cents INTEGER NOT NULL CHECK(closing_rollover_cents>=0),
    projected_next_base_cents INTEGER NOT NULL CHECK(projected_next_base_cents>=0),
    calculated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(cycle_id,budget_date),
    FOREIGN KEY(cycle_id) REFERENCES budget_cycles(id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS trg_prevent_negative_asset
BEFORE UPDATE OF balance_cents ON accounts
FOR EACH ROW WHEN NEW.is_asset=1 AND NEW.balance_cents<0
BEGIN SELECT RAISE(ABORT,'INSUFFICIENT_ASSET_BALANCE'); END;

CREATE TRIGGER IF NOT EXISTS trg_entry_no_update_after_post
BEFORE UPDATE ON ledger_entries
FOR EACH ROW WHEN (SELECT status FROM ledger_transactions WHERE id=OLD.transaction_id)<>'DRAFT'
BEGIN SELECT RAISE(ABORT,'POSTED_ENTRY_IS_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS trg_entry_no_delete_after_post
BEFORE DELETE ON ledger_entries
FOR EACH ROW WHEN (SELECT status FROM ledger_transactions WHERE id=OLD.transaction_id)<>'DRAFT'
BEGIN SELECT RAISE(ABORT,'POSTED_ENTRY_IS_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS trg_tx_immutable_after_post
BEFORE UPDATE OF kind,effective_at,local_date,note,reverses_tx_id,replaces_tx_id
ON ledger_transactions
FOR EACH ROW WHEN OLD.status<>'DRAFT'
BEGIN SELECT RAISE(ABORT,'POSTED_TRANSACTION_IS_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS trg_tx_status_transition
BEFORE UPDATE OF status ON ledger_transactions
FOR EACH ROW WHEN NOT (
  OLD.status=NEW.status OR
  (OLD.status='DRAFT' AND NEW.status='POSTED') OR
  (OLD.status='POSTED' AND NEW.status='REVERSED')
)
BEGIN SELECT RAISE(ABORT,'INVALID_TRANSACTION_STATE'); END;

CREATE TRIGGER IF NOT EXISTS trg_validate_before_post
BEFORE UPDATE OF status ON ledger_transactions
FOR EACH ROW WHEN OLD.status='DRAFT' AND NEW.status='POSTED' AND (
  (SELECT COUNT(*) FROM ledger_entries WHERE transaction_id=NEW.id)<2 OR
  COALESCE((SELECT SUM(amount_cents) FROM ledger_entries WHERE transaction_id=NEW.id),0)<>0
)
BEGIN SELECT RAISE(ABORT,'UNBALANCED_TRANSACTION'); END;

CREATE TRIGGER IF NOT EXISTS trg_apply_posted_transaction
AFTER UPDATE OF status ON ledger_transactions
FOR EACH ROW WHEN OLD.status='DRAFT' AND NEW.status='POSTED'
BEGIN
  UPDATE accounts
  SET balance_cents=balance_cents+(
        SELECT SUM(amount_cents) FROM ledger_entries
        WHERE transaction_id=NEW.id AND account_id=accounts.id
      ),
      version=version+1
  WHERE EXISTS (
    SELECT 1 FROM ledger_entries
    WHERE transaction_id=NEW.id AND account_id=accounts.id
  );
END;

CREATE VIEW IF NOT EXISTS v_main_wallet AS
SELECT COALESCE(SUM(balance_cents),0) AS total_balance_cents
FROM accounts WHERE is_asset=1;

CREATE VIEW IF NOT EXISTS v_account_integrity AS
SELECT a.id,a.name,a.balance_cents AS cached_balance_cents,
       COALESCE(SUM(CASE WHEN t.status IN ('POSTED','REVERSED')
                         THEN e.amount_cents ELSE 0 END),0) AS ledger_balance_cents,
       a.balance_cents-COALESCE(SUM(CASE WHEN t.status IN ('POSTED','REVERSED')
                         THEN e.amount_cents ELSE 0 END),0) AS difference_cents
FROM accounts a
LEFT JOIN ledger_entries e ON e.account_id=a.id
LEFT JOIN ledger_transactions t ON t.id=e.transaction_id
GROUP BY a.id;
"""


class FinanceError(Exception):
    pass


class ValidationError(FinanceError):
    pass


@dataclass(frozen=True)
class BudgetInfo:
    today_available_cents: int
    today_spent_cents: int
    base_cents: int
    rollover_open_cents: int
    next_base_cents: int
    tomorrow_available_cents: int
    future_pool_cents: int
    expired: bool = False


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money_to_cents(text: str) -> int:
    try:
        value = Decimal(text.strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError):
        raise ValidationError("请输入正确的金额") from None
    if not value.is_finite() or value <= 0:
        raise ValidationError("金额必须大于 0")
    if value > Decimal("999999999.99"):
        raise ValidationError("金额过大")
    return int(value * 100)


def format_money(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def normalized_name(name: str) -> tuple[str, str]:
    display = unicodedata.normalize("NFKC", name).strip()
    if not display:
        raise ValidationError("钱包名称不能为空")
    if len(display) > 30:
        raise ValidationError("钱包名称不能超过 30 个字符")
    return display, display.casefold()


def daily_slice(pool: int, days: int) -> int:
    if days <= 0 or pool <= 0:
        return 0
    q, r = divmod(pool, days)
    return q + (1 if r else 0)


class FinanceDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, autocommit=True, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._seed()

    def close(self):
        self.conn.close()

    @contextmanager
    def write(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.assert_integrity()
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _seed(self):
        with self.write():
            self.conn.executemany(
                """INSERT OR IGNORE INTO accounts(id,name,name_key,kind,is_asset)
                   VALUES(?,?,?,?,?)""",
                [
                    ("sys-unallocated", "未分配资金", "__unallocated__", "UNALLOCATED", 1),
                    ("sys-income", "收入", "__income__", "INCOME_CLEARING", 0),
                    ("sys-expense", "消费", "__expense__", "EXPENSE_CLEARING", 0),
                ],
            )
            self.conn.execute("INSERT OR IGNORE INTO app_settings VALUES('safe_balance_cents','10000')")

    def assert_integrity(self):
        if self.conn.execute(
            "SELECT 1 FROM v_account_integrity WHERE difference_cents<>0 LIMIT 1"
        ).fetchone():
            raise RuntimeError("数据库余额与流水不一致")
        if self.conn.execute(
            "SELECT 1 FROM accounts WHERE is_asset=1 AND balance_cents<0 LIMIT 1"
        ).fetchone():
            raise RuntimeError("检测到负余额")

    def post(self, kind: str, entries: list[tuple[str, int, str]], *,
             local_date: date | None = None, note: str = "",
             reverses: str | None = None, replaces: str | None = None) -> str:
        if len(entries) < 2 or sum(x[1] for x in entries) != 0:
            raise ValidationError("分录不平衡")
        txid = new_id()
        d = local_date or date.today()
        self.conn.execute(
            """INSERT INTO ledger_transactions
               (id,kind,status,effective_at,local_date,note,reverses_tx_id,replaces_tx_id)
               VALUES(?,?,'DRAFT',?,?,?,?,?)""",
            (txid, kind, now_iso(), d.isoformat(), note.strip(), reverses, replaces),
        )
        self.conn.executemany(
            """INSERT INTO ledger_entries(transaction_id,account_id,amount_cents,line_role)
               VALUES(?,?,?,?)""",
            [(txid, *entry) for entry in entries],
        )
        self.conn.execute("UPDATE ledger_transactions SET status='POSTED' WHERE id=?", (txid,))
        return txid

    def setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.write():
            self.conn.execute(
                """INSERT INTO app_settings(setting_key,setting_value) VALUES(?,?)
                   ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
                (key, value),
            )

    def totals(self):
        total = self.conn.execute("SELECT total_balance_cents FROM v_main_wallet").fetchone()[0]
        unallocated = self.conn.execute(
            "SELECT balance_cents FROM accounts WHERE kind='UNALLOCATED'"
        ).fetchone()[0]
        return total, unallocated

    def wallets(self):
        return self.conn.execute(
            """SELECT a.*,sg.target_cents
               FROM accounts a LEFT JOIN savings_goals sg ON sg.wallet_id=a.id
               WHERE a.kind IN ('BUDGET','SAVINGS') AND a.archived_at IS NULL
               ORDER BY a.created_at"""
        ).fetchall()

    def account(self, account_id: str):
        row = self.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise ValidationError("钱包不存在")
        return row

    def add_income(self, cents: int, note: str = ""):
        with self.write():
            return self.post("INCOME", [
                ("sys-unallocated", cents, "DESTINATION"),
                ("sys-income", -cents, "INCOME"),
            ], note=note)

    def create_budget(self, name: str, cents: int, days: int):
        display, key = normalized_name(name)
        if not 1 <= days <= 366:
            raise ValidationError("周期天数必须为 1～366 天")
        today = date.today()
        with self.write():
            if self.conn.execute("SELECT 1 FROM accounts WHERE name_key=?", (key,)).fetchone():
                raise ValidationError("钱包名称已存在")
            if self.account("sys-unallocated")["balance_cents"] < cents:
                raise ValidationError("初始资金不能超过未分配资金")
            wid, cid = new_id(), new_id()
            self.conn.execute(
                "INSERT INTO accounts(id,name,name_key,kind,is_asset) VALUES(?,?,?,'BUDGET',1)",
                (wid, display, key),
            )
            self.conn.execute(
                """INSERT INTO budget_cycles
                   (id,wallet_id,initial_amount_cents,start_date,end_date)
                   VALUES(?,?,?,?,?)""",
                (cid, wid, cents, today.isoformat(), (today + timedelta(days=days)).isoformat()),
            )
            self.post("ALLOCATION", [
                ("sys-unallocated", -cents, "SOURCE"),
                (wid, cents, "DESTINATION"),
            ])
            self.rebuild_budget(wid)
            return wid

    def create_savings(self, name: str, initial: int, target: int):
        display, key = normalized_name(name)
        if target <= 0 or initial < 0 or initial > target:
            raise ValidationError("请检查初始金额和目标金额")
        with self.write():
            if self.conn.execute("SELECT 1 FROM accounts WHERE name_key=?", (key,)).fetchone():
                raise ValidationError("钱包名称已存在")
            if self.account("sys-unallocated")["balance_cents"] < initial:
                raise ValidationError("初始资金不能超过未分配资金")
            wid = new_id()
            self.conn.execute(
                "INSERT INTO accounts(id,name,name_key,kind,is_asset) VALUES(?,?,?,'SAVINGS',1)",
                (wid, display, key),
            )
            self.conn.execute(
                "INSERT INTO savings_goals(wallet_id,target_cents) VALUES(?,?)", (wid, target)
            )
            if initial:
                self.post("ALLOCATION", [
                    ("sys-unallocated", -initial, "SOURCE"),
                    (wid, initial, "DESTINATION"),
                ])
            return wid

    def _cycle(self, wallet_id: str):
        return self.conn.execute(
            "SELECT * FROM budget_cycles WHERE wallet_id=? AND status='ACTIVE'", (wallet_id,)
        ).fetchone()

    def _events(self, wallet_id: str, start: date, through: date):
        return self.conn.execute(
            """SELECT t.local_date,t.effective_at,t.kind,e.amount_cents
               FROM ledger_transactions t JOIN ledger_entries e ON e.transaction_id=t.id
               WHERE e.account_id=? AND t.status='POSTED'
                 AND t.kind IN ('EXPENSE','TRANSFER','SAVINGS_WITHDRAWAL')
                 AND t.local_date BETWEEN ? AND ?
               ORDER BY t.local_date,t.effective_at,t.id""",
            (wallet_id, start.isoformat(), through.isoformat()),
        ).fetchall()

    def rebuild_budget(self, wallet_id: str, through: date | None = None):
        cycle = self._cycle(wallet_id)
        if not cycle:
            return
        start = date.fromisoformat(cycle["start_date"])
        end = date.fromisoformat(cycle["end_date"])
        through = min(through or date.today(), end - timedelta(days=1))
        if through < start:
            return
        by_day: dict[str, list] = {}
        for event in self._events(wallet_id, start, through):
            by_day.setdefault(event["local_date"], []).append(event)
        self.conn.execute("DELETE FROM budget_daily_state WHERE cycle_id=?", (cycle["id"],))
        pool, rollover = cycle["initial_amount_cents"], 0
        day = start
        while day <= through:
            days_including = (end - day).days
            opening_pool, opening_roll = pool, rollover
            base = daily_slice(pool, days_including)
            pool -= base
            base_rem, roll_rem = base, rollover
            spent = transfer_in = transfer_out = overrun = 0
            for event in by_day.get(day.isoformat(), []):
                amount = event["amount_cents"]
                if event["kind"] == "EXPENSE" and amount < 0:
                    cost = -amount
                    spent += cost
                    take = min(cost, base_rem); base_rem -= take; cost -= take
                    take = min(cost, roll_rem); roll_rem -= take; cost -= take
                    if cost:
                        if cost > pool:
                            raise ValidationError("历史流水会造成预算负数")
                        pool -= cost; overrun += cost
                elif amount > 0:
                    pool += amount; transfer_in += amount
                elif amount < 0:
                    cost = -amount; transfer_out += cost
                    take = min(cost, pool); pool -= take; cost -= take
                    take = min(cost, roll_rem); roll_rem -= take; cost -= take
                    take = min(cost, base_rem); base_rem -= take; cost -= take
                    if cost:
                        raise ValidationError("历史转账会造成预算负数")
            rollover = base_rem + roll_rem
            future_days = days_including - 1
            next_base = daily_slice(pool, future_days)
            self.conn.execute(
                """INSERT INTO budget_daily_state VALUES
                   (?,?,?,?,?,?,?,?,?,?,?, ?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))""",
                (cycle["id"], day.isoformat(), opening_pool, base, opening_roll,
                 spent, transfer_in, transfer_out, overrun, pool, rollover, next_base),
            )
            day += timedelta(days=1)

    def budget_info(self, wallet_id: str) -> BudgetInfo:
        cycle = self._cycle(wallet_id)
        if not cycle:
            return BudgetInfo(0, 0, 0, 0, 0, 0, 0, True)
        end = date.fromisoformat(cycle["end_date"])
        if date.today() >= end:
            bal = self.account(wallet_id)["balance_cents"]
            return BudgetInfo(bal, 0, 0, bal, 0, 0, 0, True)
        self.rebuild_budget(wallet_id)
        row = self.conn.execute(
            """SELECT * FROM budget_daily_state
               WHERE cycle_id=? AND budget_date=?""",
            (cycle["id"], date.today().isoformat()),
        ).fetchone()
        available = row["closing_rollover_cents"]
        tomorrow = available + row["projected_next_base_cents"]
        return BudgetInfo(
            available, row["expense_cents"], row["base_budget_cents"],
            row["opening_rollover_cents"], row["projected_next_base_cents"],
            tomorrow, row["closing_future_pool_cents"], False,
        )

    def expense_preview(self, wallet_id: str, cents: int):
        wallet = self.account(wallet_id)
        if wallet["kind"] != "BUDGET":
            raise ValidationError("只能从周期预算钱包记消费")
        if cents > wallet["balance_cents"]:
            raise ValidationError("钱包总余额不足")
        info = self.budget_info(wallet_id)
        overrun = max(0, cents - info.today_available_cents)
        future = info.future_pool_cents - overrun
        cycle = self._cycle(wallet_id)
        future_days = max(0, (date.fromisoformat(cycle["end_date"]) - date.today()).days - 1)
        return overrun, daily_slice(future, future_days)

    def add_expense(self, wallet_id: str, cents: int, note: str = ""):
        self.expense_preview(wallet_id, cents)
        with self.write():
            txid = self.post("EXPENSE", [
                (wallet_id, -cents, "SOURCE"),
                ("sys-expense", cents, "EXPENSE"),
            ], note=note)
            self.rebuild_budget(wallet_id)
            return txid

    def transfer_preview(self, source_id: str, cents: int) -> int | None:
        source = self.account(source_id)
        if cents > source["balance_cents"]:
            raise ValidationError("转出钱包余额不足")
        if source["kind"] != "BUDGET":
            return None
        info = self.budget_info(source_id)
        future_after = max(0, info.future_pool_cents - cents)
        cycle = self._cycle(source_id)
        days = max(0, (date.fromisoformat(cycle["end_date"]) - date.today()).days - 1)
        return daily_slice(future_after, days)

    def transfer(self, source_id: str, target_id: str, cents: int):
        if source_id == target_id:
            raise ValidationError("不能转给同一个钱包")
        with self.write():
            source, target = self.account(source_id), self.account(target_id)
            if cents > source["balance_cents"]:
                raise ValidationError("转出钱包余额不足")
            kind = "SAVINGS_WITHDRAWAL" if source["kind"] == "SAVINGS" else "TRANSFER"
            txid = self.post(kind, [
                (source_id, -cents, "SOURCE"),
                (target_id, cents, "DESTINATION"),
            ])
            if source["kind"] == "BUDGET": self.rebuild_budget(source_id)
            if target["kind"] == "BUDGET": self.rebuild_budget(target_id)
            return txid

    def history(self, limit: int = 200):
        return self.conn.execute(
            """SELECT t.id,t.local_date,t.kind,t.note,t.status,
                      GROUP_CONCAT(CASE WHEN a.is_asset=1 THEN a.name END,' → ') names,
                      SUM(CASE WHEN a.is_asset=1 THEN e.amount_cents ELSE 0 END) net_asset,
                      MIN(CASE WHEN a.is_asset=1 THEN e.amount_cents END) min_asset,
                      MAX(CASE WHEN a.is_asset=1 THEN e.amount_cents END) max_asset
               FROM ledger_transactions t
               JOIN ledger_entries e ON e.transaction_id=t.id
               JOIN accounts a ON a.id=e.account_id
               WHERE t.status='POSTED' AND t.kind<>'REVERSAL'
               GROUP BY t.id ORDER BY t.local_date DESC,t.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def wallet_history(self, wallet_id: str, limit: int = 100):
        return self.conn.execute(
            """SELECT t.id,t.local_date,t.kind,t.note,e.amount_cents
               FROM ledger_transactions t JOIN ledger_entries e ON e.transaction_id=t.id
               WHERE e.account_id=? AND t.status='POSTED' AND t.kind<>'REVERSAL'
               ORDER BY t.local_date DESC,t.created_at DESC LIMIT ?""",
            (wallet_id, limit),
        ).fetchall()

    def delete_transaction(self, txid: str):
        with self.write():
            tx = self.conn.execute(
                "SELECT * FROM ledger_transactions WHERE id=? AND status='POSTED'", (txid,)
            ).fetchone()
            if not tx:
                raise ValidationError("流水不存在或已撤销")
            entries = self.conn.execute(
                "SELECT * FROM ledger_entries WHERE transaction_id=?", (txid,)
            ).fetchall()
            affected = [e["account_id"] for e in entries if self.account(e["account_id"])["kind"] == "BUDGET"]
            self.post("REVERSAL", [
                (e["account_id"], -e["amount_cents"], "REVERSAL") for e in entries
            ], local_date=date.fromisoformat(tx["local_date"]), note=f"冲销 {txid}", reverses=txid)
            self.conn.execute("UPDATE ledger_transactions SET status='REVERSED' WHERE id=?", (txid,))
            for wid in set(affected):
                self.rebuild_budget(wid)

    def edit_transaction(self, txid: str, new_cents: int, note: str = ""):
        if new_cents <= 0:
            raise ValidationError("金额必须大于 0")
        with self.write():
            tx = self.conn.execute(
                "SELECT * FROM ledger_transactions WHERE id=? AND status='POSTED'", (txid,)
            ).fetchone()
            if not tx:
                raise ValidationError("流水不存在或已撤销")
            if tx["kind"] == "ALLOCATION":
                raise ValidationError("初始划拨不能直接修改，请通过钱包转账调整")
            entries = self.conn.execute(
                "SELECT * FROM ledger_entries WHERE transaction_id=? ORDER BY id", (txid,)
            ).fetchall()
            affected = {
                e["account_id"] for e in entries
                if self.account(e["account_id"])["kind"] == "BUDGET"
            }
            reversal_id = self.post(
                "REVERSAL",
                [(e["account_id"], -e["amount_cents"], "REVERSAL") for e in entries],
                local_date=date.fromisoformat(tx["local_date"]),
                note=f"修改冲销 {txid}",
                reverses=txid,
            )
            self.conn.execute("UPDATE ledger_transactions SET status='REVERSED' WHERE id=?", (txid,))
            replacement_entries = [
                (e["account_id"], new_cents if e["amount_cents"] > 0 else -new_cents, e["line_role"])
                for e in entries
            ]
            new_id_value = self.post(
                tx["kind"], replacement_entries,
                local_date=date.fromisoformat(tx["local_date"]),
                note=note, replaces=txid,
            )
            for wid in affected:
                self.rebuild_budget(wid)
            return new_id_value, reversal_id

