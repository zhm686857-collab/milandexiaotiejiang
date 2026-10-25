from __future__ import annotations

import sys
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from finance_core import FinanceDB, FinanceError, format_money, money_to_cents


APP_NAME = "清简账本"


def app_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def db_path() -> Path:
    return app_dir() / "personal_finance.db"


def safe_call(fn):
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FinanceError as exc:
            messagebox.showerror("无法完成", str(exc))
        except Exception as exc:
            messagebox.showerror("程序错误", f"操作已撤销，数据没有被部分修改。\n\n{exc}")
    return wrapped


class FormDialog(ctk.CTkToplevel):
    def __init__(self, app, title, fields, submit_text="确认"):
        super().__init__(app)
        self.app, self.result, self.inputs = app, None, {}
        self.title(title); self.geometry("430x520"); self.resizable(False, False)
        self.transient(app); self.grab_set()
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w", padx=28, pady=(25, 18))
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28)
        for key, label, default, kind in fields:
            ctk.CTkLabel(body, text=label, text_color=("#555", "#bbb")).pack(anchor="w", pady=(8, 4))
            if kind == "choice":
                widget = ctk.CTkOptionMenu(body, values=list(default.keys()))
                widget._value_map = default
            else:
                widget = ctk.CTkEntry(body, height=38, placeholder_text=str(default or ""))
            widget.pack(fill="x")
            self.inputs[key] = widget
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=28, pady=24)
        ctk.CTkButton(buttons, text="取消", fg_color="transparent", border_width=1,
                      text_color=("#333", "#ddd"), command=self.destroy).pack(side="left", expand=True, fill="x", padx=(0, 8))
        ctk.CTkButton(buttons, text=submit_text, command=self.submit).pack(side="left", expand=True, fill="x", padx=(8, 0))
        self.bind("<Return>", lambda _e: self.submit())

    def submit(self):
        out = {}
        for key, widget in self.inputs.items():
            value = widget.get()
            out[key] = widget._value_map[value] if hasattr(widget, "_value_map") else value
        self.result = out
        self.destroy()

    def show(self):
        self.wait_window()
        return self.result


class FinanceApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.title(APP_NAME)
        self.geometry("1040x720")
        self.minsize(880, 620)
        try:
            self.db = FinanceDB(db_path())
        except Exception as exc:
            messagebox.showerror("无法打开数据库", f"请确认程序所在目录可写。\n\n{exc}")
            self.destroy(); return
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.tabs = ctk.CTkTabview(self, anchor="w")
        self.tabs.pack(fill="both", expand=True, padx=18, pady=18)
        for title in ("总览", "流水", "设置"):
            self.tabs.add(title)
        self.build_overview(); self.build_history(); self.build_settings(); self.refresh()

    def clear(self, frame):
        for child in frame.winfo_children(): child.destroy()

    def build_overview(self):
        tab = self.tabs.tab("总览")
        self.header = ctk.CTkFrame(tab, corner_radius=18)
        self.header.pack(fill="x", padx=10, pady=10)
        self.total_label = ctk.CTkLabel(self.header, text="", font=ctk.CTkFont(size=34, weight="bold"))
        self.total_label.grid(row=1, column=0, sticky="w", padx=24)
        ctk.CTkLabel(self.header, text="总资产", text_color="gray").grid(row=0, column=0, sticky="w", padx=24, pady=(18, 0))
        self.unallocated_label = ctk.CTkLabel(self.header, text="")
        self.unallocated_label.grid(row=2, column=0, sticky="w", padx=24, pady=(4, 20))
        actions = ctk.CTkFrame(self.header, fg_color="transparent")
        actions.grid(row=0, column=1, rowspan=3, sticky="e", padx=20)
        for text, command in (("＋ 收入", self.add_income), ("记一笔", self.add_expense),
                              ("新建钱包", self.new_wallet), ("转账", self.transfer)):
            ctk.CTkButton(actions, text=text, width=105, command=command).pack(side="left", padx=5)
        self.header.grid_columnconfigure(0, weight=1)
        self.cards = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.cards.pack(fill="both", expand=True, padx=5, pady=5)

    def build_history(self):
        tab = self.tabs.tab("流水")
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(14, 4))
        ctk.CTkLabel(top, text="全部流水", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text="点击删除会以冲销方式撤销，原始记录仍保留审计", text_color="gray").pack(side="right")
        self.history_frame = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.history_frame.pack(fill="both", expand=True, padx=8, pady=8)

    def build_settings(self):
        tab = self.tabs.tab("设置")
        box = ctk.CTkFrame(tab, corner_radius=16)
        box.pack(fill="x", padx=14, pady=18)
        ctk.CTkLabel(box, text="余额安全线", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", padx=22, pady=(18, 4))
        self.safe_entry = ctk.CTkEntry(box, width=220)
        self.safe_entry.pack(anchor="w", padx=22, pady=8)
        ctk.CTkButton(box, text="保存", width=100, command=self.save_settings).pack(anchor="w", padx=22, pady=(4, 18))
        ctk.CTkLabel(tab, text=f"数据库位置：{db_path()}", text_color="gray", wraplength=800).pack(anchor="w", padx=20, pady=10)

    def refresh(self):
        total, unallocated = self.db.totals()
        safe = int(self.db.setting("safe_balance_cents", "10000"))
        self.total_label.configure(text=format_money(total), text_color="#ef4444" if total < safe else ("#111", "#f5f5f5"))
        self.unallocated_label.configure(text=f"未分配资金  {format_money(unallocated)}")
        self.safe_entry.delete(0, "end"); self.safe_entry.insert(0, f"{safe/100:.2f}")
        self.refresh_cards(); self.refresh_history()

    def refresh_cards(self):
        self.clear(self.cards)
        wallets = self.db.wallets()
        if not wallets:
            ctk.CTkLabel(self.cards, text="还没有钱包\n先录入收入，再建立第一个预算钱包。",
                         text_color="gray", font=ctk.CTkFont(size=18)).pack(pady=80)
            return
        for wallet in wallets:
            card = ctk.CTkFrame(self.cards, corner_radius=16)
            card.pack(fill="x", padx=8, pady=7)
            ctk.CTkLabel(card, text=wallet["name"], font=ctk.CTkFont(size=18, weight="bold")).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 2))
            if wallet["kind"] == "BUDGET":
                info = self.db.budget_info(wallet["id"])
                label = "周期剩余" if info.expired else "今日剩余"
                ctk.CTkLabel(card, text=label, text_color="gray").grid(row=1, column=0, sticky="w", padx=20)
                ctk.CTkLabel(card, text=format_money(info.today_available_cents), font=ctk.CTkFont(size=30, weight="bold")).grid(row=2, column=0, sticky="w", padx=20)
                helper = "周期已结束" if info.expired else f"今日已花 {format_money(info.today_spent_cents)}　预计明日可用 {format_money(info.tomorrow_available_cents)}"
                ctk.CTkLabel(card, text=helper, text_color="gray").grid(row=3, column=0, sticky="w", padx=20, pady=(3, 16))
            else:
                target = wallet["target_cents"] or 1
                ctk.CTkLabel(card, text=f"{format_money(wallet['balance_cents'])} / {format_money(target)}", font=ctk.CTkFont(size=27, weight="bold")).grid(row=1, column=0, sticky="w", padx=20, pady=6)
                bar = ctk.CTkProgressBar(card, width=420)
                bar.set(min(1, wallet["balance_cents"] / target)); bar.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 18))
            ctk.CTkLabel(card, text=f"钱包总余额 {format_money(wallet['balance_cents'])}", text_color="gray").grid(row=0, column=1, sticky="e", padx=16)
            ctk.CTkButton(card, text="详情 ›", width=82, fg_color="transparent", border_width=1,
                          command=lambda wid=wallet["id"]: self.wallet_detail(wid)).grid(row=1, column=1, rowspan=2, padx=18)
            card.grid_columnconfigure(0, weight=1)

    def refresh_history(self):
        self.clear(self.history_frame)
        for row in self.db.history():
            item = ctk.CTkFrame(self.history_frame, corner_radius=12)
            item.pack(fill="x", padx=5, pady=4)
            title = {"INCOME":"收入","EXPENSE":"消费","TRANSFER":"转账","ALLOCATION":"划拨","SAVINGS_WITHDRAWAL":"储蓄提现"}.get(row["kind"], row["kind"])
            ctk.CTkLabel(item, text=f"{row['local_date']}  {title}", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=14, pady=13)
            ctk.CTkLabel(item, text=row["note"] or row["names"] or "", text_color="gray").pack(side="left", padx=8)
            amount = row["net_asset"] if row["kind"] in ("INCOME", "EXPENSE") else abs(row["min_asset"] or row["max_asset"] or 0)
            ctk.CTkLabel(item, text=format_money(amount), font=ctk.CTkFont(weight="bold")).pack(side="right", padx=(6, 2))
            ctk.CTkButton(item, text="删除", width=58, fg_color="transparent", border_width=1,
                          border_color="#ef4444", text_color="#ef4444",
                          command=lambda tid=row["id"]: self.delete_tx(tid)).pack(side="right", padx=12)
            if row["kind"] != "ALLOCATION":
                ctk.CTkButton(item, text="修改", width=58, fg_color="transparent", border_width=1,
                              command=lambda tid=row["id"]: self.edit_tx(tid)).pack(side="right", padx=2)

    @safe_call
    def add_income(self):
        data = FormDialog(self, "录入收入", [("amount","金额（元）","例如 2000","text"), ("note","备注（可选）","生活费","text")], "确认收入").show()
        if not data: return
        self.db.add_income(money_to_cents(data["amount"]), data["note"]); self.refresh()

    @safe_call
    def new_wallet(self):
        kind = messagebox.askyesno("钱包类型", "建立周期预算钱包？\n\n选择“否”将建立储蓄罐。")
        if kind:
            data = FormDialog(self, "新建周期预算", [("name","钱包名称","餐饮","text"), ("amount","初始资金（元）","900","text"), ("days","周期天数","30","text")], "创建钱包").show()
            if not data: return
            cents, days = money_to_cents(data["amount"]), int(data["days"])
            if not messagebox.askokcancel("确认", f"预计每日预算：{format_money(cents // days)} / 天"): return
            self.db.create_budget(data["name"], cents, days)
        else:
            data = FormDialog(self, "新建储蓄罐", [("name","名称","旅行基金","text"), ("amount","初始资金（元）","100","text"), ("target","目标金额（元）","5000","text")], "创建储蓄罐").show()
            if not data: return
            self.db.create_savings(data["name"], money_to_cents(data["amount"]), money_to_cents(data["target"]))
        self.refresh()

    @safe_call
    def add_expense(self):
        budgets = {w["name"]: w["id"] for w in self.db.wallets() if w["kind"] == "BUDGET"}
        if not budgets: raise FinanceError("请先创建周期预算钱包")
        data = FormDialog(self, "记一笔消费", [("wallet","预算钱包",budgets,"choice"), ("amount","金额（元）","28","text"), ("note","备注（可选）","午餐","text")], "记账").show()
        if not data: return
        cents = money_to_cents(data["amount"])
        overrun, next_base = self.db.expense_preview(data["wallet"], cents)
        if overrun and not messagebox.askokcancel("超支提醒", f"超支！将扣除未分配余额，明日预算将重算为 {format_money(next_base)}。\n\n是否继续？", icon="warning"):
            return
        self.db.add_expense(data["wallet"], cents, data["note"]); self.refresh()

    @safe_call
    def transfer(self):
        accounts = {"未分配资金": "sys-unallocated"} | {w["name"]: w["id"] for w in self.db.wallets()}
        if len(accounts) < 2: raise FinanceError("至少需要两个钱包")
        data = FormDialog(self, "钱包转账", [("source","转出钱包",accounts,"choice"), ("target","转入钱包",accounts,"choice"), ("amount","金额（元）","100","text")], "确认转账").show()
        if not data: return
        cents = money_to_cents(data["amount"])
        next_base = self.db.transfer_preview(data["source"], cents)
        src = self.db.account(data["source"])
        if src["kind"] == "SAVINGS" and not messagebox.askyesno("储蓄提现", "这是储蓄罐资金。确定要转出吗？", icon="warning"): return
        if next_base is not None and next_base < 500 and not messagebox.askyesno("低预算提醒", f"转出后每天预算约为 {format_money(next_base)}，确定转出吗？", icon="warning"): return
        self.db.transfer(data["source"], data["target"], cents); self.refresh()

    def wallet_detail(self, wallet_id):
        wallet = self.db.account(wallet_id)
        win = ctk.CTkToplevel(self); win.title(wallet["name"]); win.geometry("620x560"); win.transient(self)
        ctk.CTkLabel(win, text=wallet["name"], font=ctk.CTkFont(size=26, weight="bold")).pack(anchor="w", padx=24, pady=(22, 4))
        if wallet["kind"] == "BUDGET":
            info = self.db.budget_info(wallet_id)
            ctk.CTkLabel(win, text=f"每日基础预算 {format_money(info.base_cents)}    结转池 {format_money(info.rollover_open_cents)}", text_color="gray").pack(anchor="w", padx=24)
        ctk.CTkLabel(win, text=f"钱包总余额 {format_money(wallet['balance_cents'])}").pack(anchor="w", padx=24, pady=(4, 12))
        body = ctk.CTkScrollableFrame(win); body.pack(fill="both", expand=True, padx=18, pady=10)
        for row in self.db.wallet_history(wallet_id):
            frame = ctk.CTkFrame(body); frame.pack(fill="x", pady=3)
            ctk.CTkLabel(frame, text=f"{row['local_date']}  {row['note'] or row['kind']}").pack(side="left", padx=12, pady=10)
            ctk.CTkLabel(frame, text=format_money(row["amount_cents"]), text_color="#ef4444" if row["amount_cents"]<0 else "#22c55e").pack(side="right", padx=12)

    @safe_call
    def delete_tx(self, txid):
        if not messagebox.askyesno("修改历史账目", "修改历史账目会导致后续每日预算全面重算，是否继续？", icon="warning"): return
        self.db.delete_transaction(txid); self.refresh()

    @safe_call
    def edit_tx(self, txid):
        if not messagebox.askyesno("修改历史账目", "修改历史账目会导致后续每日预算全面重算，是否继续？", icon="warning"):
            return
        data = FormDialog(self, "修改流水", [("amount","新金额（元）","请输入新金额","text"),
                                             ("note","新备注（可选）","","text")], "保存修改").show()
        if not data: return
        self.db.edit_transaction(txid, money_to_cents(data["amount"]), data["note"])
        self.refresh()

    @safe_call
    def save_settings(self):
        cents = money_to_cents(self.safe_entry.get())
        self.db.set_setting("safe_balance_cents", str(cents)); self.refresh()

    def close(self):
        self.db.close(); self.destroy()


if __name__ == "__main__":
    FinanceApp().mainloop()

