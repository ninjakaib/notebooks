"""
Day-level debt payoff planner.

Reads payoff_config.yaml, finds the payment schedule that minimizes total
interest given your real paydays, bills and minimum payments, then:

  1. Re-checks that schedule with an independent day-by-day simulation of the
     lender/card rules (daily interest, statements, minimums, Best Buy payment
     allocation, promo deadline, checking balance).
  2. Writes a calendar (.ics) and a CSV of every payment.

Run:
    uv run python payoff_planner.py [payoff_config.yaml]

The plan is a projection. Re-run it each month with the balances from your
latest statements and re-import the calendar.
"""

from __future__ import annotations

import calendar as cal
import csv
import ctypes
import hashlib
import math
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import holidays
import numpy as np
import yaml
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix


ONE_DAY = timedelta(days=1)

BIG_M = 100_000.0
# Objective cost per dollar of cash the plan is missing. Far above any
# interest saving, so the optimizer only "borrows" when there is no other way.
SHORTFALL_PENALTY = 100.0
# Tiny preference for paying earlier when two schedules cost the same interest.
EARLY_TIEBREAK = 1e-6
# Extra cushion the optimizer keeps above your buffer, because final payoff
# amounts come out a few cents to dollars different from the plan.
CASH_MARGIN = 5.00

# Used only when the config leaves these blank. Any use makes the plan a DRAFT.
DEFAULT_LOAN_DUE_DAY = 15
DEFAULT_STATEMENT_DAY = 20
DEFAULT_CARD_DUE_DAY = 15
# Card due dates are at least 21 days after the statement closes (CARD Act).
MIN_STATEMENT_TO_DUE_DAYS = 21

PAYCHECKS_PER_YEAR = {"weekly": 52, "biweekly": 26}


# ============================================================
# Dates
# ============================================================

def parse_date(value, where: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"{where}: can't read date {value!r} (use YYYY-MM-DD)")


def month_day(year: int, month: int, day: int) -> date:
    """The given day of a month, clamped to the month's last day."""
    return date(year, month, min(day, cal.monthrange(year, month)[1]))


def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    absolute = year * 12 + (month - 1) + n
    return absolute // 12, absolute % 12 + 1


def months_between(first: date, last: date):
    """(year, month) for every month from first's month to last's month."""
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield year, month
        year, month = add_months(year, month, 1)


def days_between(start: date, stop: date):
    """Every day in [start, stop)."""
    day = start
    while day < stop:
        yield day
        day += ONE_DAY


class BusinessDays:
    """Weekdays that aren't US federal holidays (when payments process)."""

    def __init__(self, first_year: int, last_year: int):
        self.holidays = holidays.US(years=range(first_year, last_year + 1))

    def is_open(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def add(self, day: date, n: int) -> date:
        while n > 0:
            day += ONE_DAY
            if self.is_open(day):
                n -= 1
        return day

    def subtract(self, day: date, n: int) -> date:
        while n > 0:
            day -= ONE_DAY
            if self.is_open(day):
                n -= 1
        return day

    def roll(self, day: date, rule: str) -> date:
        step = -ONE_DAY if rule == "previous" else ONE_DAY
        while not self.is_open(day):
            day += step
        return day


# ============================================================
# Plan inputs
# ============================================================

class Notes:
    """Assumptions made for missing or unsupported config values."""

    def __init__(self):
        self.assumptions: list[str] = []
        # True when a guessed date affects the calendar.
        self.draft = False

    def assume(self, message: str, affects_dates: bool = False):
        self.assumptions.append(message)
        self.draft = self.draft or affects_dates


@dataclass
class CashEvent:
    day: date
    amount: float          # + into checking, - out of checking
    kind: str              # income | bill | spending | expense
    name: str


@dataclass
class Cycle:
    """A minimum-payment window: payments posting in (opens, due] count."""
    opens: date
    due: date
    # Statement closing date (cards). None for loans or for a statement that
    # was issued before the plan starts.
    statement: date | None = None
    # For a statement already issued: its minimum and balances.
    known_minimum: float | None = None
    known_balance: dict[str, float] | None = None


@dataclass
class Debt:
    name: str
    kind: str                                   # installment_loan | credit_card
    initial: dict[str, float]                   # bucket -> balance
    daily_rate: dict[str, Callable[[date], float]]
    statements: list[date]
    cycles: list[Cycle]
    minimum: dict
    promo_bucket: str | None = None
    promo_pay_off_by: date | None = None
    promo_expiration: date | None = None
    # Payments made after this date (and through expiration) apply their
    # excess to the promo balance first: the last two billing cycles.
    promo_excess_from: date | None = None
    missed_promo_cost: float | None = None


@dataclass
class Inputs:
    as_of: date
    start: date
    end: date
    lag: int
    monthly_cap: float | None
    buffer: float
    checking: float
    timezone: str
    paydays: list[date]
    cash_events: list[CashEvent]
    debts: list[Debt]
    calendar: dict
    bd: BusinessDays
    notes: Notes


def require(value, where: str, missing: list[str]):
    if value is None:
        missing.append(where)
    return value


def load_inputs(cfg: dict) -> Inputs:
    notes = Notes()
    missing: list[str] = []

    settings = cfg["settings"]
    checking = cfg["checking"]
    start = parse_date(settings["plan_start"], "settings.plan_start")
    end = parse_date(settings["plan_end"], "settings.plan_end")
    as_of = parse_date(require(checking.get("balance_as_of"), "checking.balance_as_of", missing),
                       "checking.balance_as_of")
    balance = require(checking.get("balance"), "checking.balance", missing)
    buffer = require(checking.get("minimum_buffer"), "checking.minimum_buffer", missing)
    per_month = require(cfg.get("spending", {}).get("per_month"), "spending.per_month", missing)

    if missing:
        raise SystemExit("Fill in these config values first:\n  " + "\n  ".join(missing))
    assert start and end and as_of and balance is not None and buffer is not None
    as_of = min(as_of, start)
    bd = BusinessDays(as_of.year, end.year + 1)

    paydays, cash_events = load_cash_flow(cfg, as_of, end, bd, float(per_month or 0), notes)
    debts = [load_debt(raw, as_of, start, end, int(settings["payment_posting_days"]), bd, notes)
             for raw in cfg["debts"]]

    return Inputs(
        as_of=as_of,
        start=start,
        end=end,
        lag=int(settings["payment_posting_days"]),
        monthly_cap=settings.get("max_monthly_debt_payments"),
        buffer=float(buffer),
        checking=float(balance),
        timezone=settings.get("timezone") or "UTC",
        paydays=paydays,
        cash_events=cash_events,
        debts=debts,
        calendar=cfg.get("calendar", {}),
        bd=bd,
        notes=notes,
    )


def load_cash_flow(cfg, as_of, end, bd, spending_per_month, notes):
    events: list[CashEvent] = []
    all_paydays: set[date] = set()
    paychecks_per_year = 0

    for i, income in enumerate(cfg["income"]):
        where = f"income[{i}] ({income.get('name')})"
        freq = income["frequency"]
        rule = income.get("weekend_rule", "previous")
        raw_days: list[date] = []

        if freq in PAYCHECKS_PER_YEAR:
            step = timedelta(days=7 if freq == "weekly" else 14)
            day = parse_date(income["anchor_date"], f"{where}.anchor_date")
            while day > as_of:
                day -= step
            while day <= end + timedelta(days=7):
                raw_days.append(day)
                day += step
            paychecks_per_year += PAYCHECKS_PER_YEAR[freq]
        elif freq in ("semimonthly", "monthly"):
            for year, month in months_between(as_of, end):
                raw_days += [month_day(year, month, d) for d in income["days"]]
            paychecks_per_year += 12 * len(income["days"])
        else:
            raise SystemExit(f"{where}: unknown frequency {freq!r}")

        for day in raw_days:
            day = bd.roll(day, rule)
            if as_of < day <= end:
                all_paydays.add(day)
                events.append(CashEvent(day, float(income["amount"]), "income", income["name"]))

    per_paycheck = spending_per_month * 12 / paychecks_per_year
    for day in sorted(all_paydays):
        events.append(CashEvent(day, -per_paycheck, "spending", "Spending money (set aside)"))

    for bill in cfg.get("bills") or []:
        freq = bill.get("frequency", "monthly")
        every = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}[freq]
        amount = float(bill["amount"])
        due_month = bill.get("due_month")
        name = bill["name"]
        if every > 1 and due_month is None:
            notes.assume(
                f"{name}: {freq} bill has no `due_month`, so ${amount / every:,.2f} is set aside "
                f"every month on day {bill['due_day']} instead of paying ${amount:,.2f} at once. "
                f"Add `due_month: <1-12>` to model the real due date."
            )
            amount, every, due_month = amount / every, 1, None
        for year, month in months_between(as_of, end):
            if every > 1 and (month - due_month) % every:
                continue
            day = month_day(year, month, int(bill["due_day"]))
            if as_of < day <= end:
                events.append(CashEvent(day, -amount, "bill", name))

    for expense in cfg.get("one_time_expenses") or []:
        day = parse_date(expense["date"], f"one_time_expenses ({expense['name']})")
        if as_of < day <= end:
            events.append(CashEvent(day, -float(expense["amount"]), "expense", expense["name"]))

    return sorted(all_paydays), sorted(events, key=lambda e: e.day)


def load_minimum_rule(raw, notes) -> tuple[dict, float]:
    """Return (rule, minimum on the statement already issued)."""
    mp = raw["minimum_payment"]
    kind = mp["type"]
    if kind == "fixed":
        return {"type": "fixed", "amount": float(mp["amount"])}, float(mp["amount"])

    current = mp.get("current_amount")
    percent, floor = mp.get("percent"), mp.get("floor")
    if percent is None:
        percent = 1.0
        notes.assume(f"{raw['name']}: minimum-payment percent missing; assumed {percent:g}% of the "
                     f"statement balance{' plus interest' if kind == 'percent_plus_interest' else ''}.")
    if floor is None:
        floor = current if current is not None else 35.0
        notes.assume(f"{raw['name']}: minimum-payment floor missing; assumed ${floor:,.2f}.")
    rule = {"type": kind, "percent": float(percent), "floor": float(floor)}
    # A payment plan that holds the minimum at `current_amount` for statements
    # closing on or before `fixed_until`, then switches to the formula.
    fixed_until = parse_date(mp.get("fixed_until"), f"{raw['name']}.minimum_payment.fixed_until")
    if fixed_until is not None:
        if current is None:
            raise SystemExit(f"{raw['name']}: minimum_payment.fixed_until needs current_amount")
        rule["fixed_until"] = fixed_until
        rule["fixed_amount"] = float(current)
    return rule, float(current if current is not None else floor)


def load_debt(raw, as_of, start, end, lag, bd, notes) -> Debt:
    name = raw["name"]
    kind = raw["kind"]
    apr = float(raw["apr"])

    if raw.get("balance_as_of") is None:
        notes.assume(f"{name}: balance date missing; assumed the balance is current as of {as_of}.")
    elif parse_date(raw["balance_as_of"], f"{name}.balance_as_of") != as_of:
        notes.assume(f"{name}: balance date differs from the checking balance date; "
                     f"treated as the balance on {as_of}.")

    rule, current_minimum = load_minimum_rule(raw, notes)

    def pick_day(key, default, label):
        if raw.get(key) is None:
            notes.assume(f"{name}: {label} missing; assumed day {default} of each month.",
                         affects_dates=True)
            return default
        return int(raw[key])

    if kind == "installment_loan":
        if raw.get("interest_method", "daily_simple") != "daily_simple":
            raise SystemExit(f"{name}: only interest_method: daily_simple is supported for loans")
        if raw.get("extra_payments_to_principal") is None:
            notes.assume(f"{name}: assumed extra payments go to principal. Confirm with the lender. "
                         f"If they're applied as prepaid future installments instead, this plan's "
                         f"interest savings won't happen.")
        elif raw["extra_payments_to_principal"] is False:
            notes.assume(f"{name}: lender does NOT apply extra payments to principal, so paying "
                         f"extra here may not save interest. The plan still assumes it does.")

        due_day = pick_day("due_day", DEFAULT_LOAN_DUE_DAY, "due day")
        dues = [month_day(y, m, due_day) for y, m in months_between(as_of, end)]
        dues = [d for d in dues if as_of < d <= end]
        cycles = [Cycle(opens=prev, due=due) for prev, due in zip([as_of] + dues, dues)]
        rate = apr / 100 / 365
        debt = Debt(name, kind, {"principal": float(raw["balance"])},
                    {"principal": lambda _day, r=rate: r}, [], cycles, rule)

    elif kind == "credit_card":
        if raw.get("interest_method", "average_daily_balance") != "average_daily_balance":
            raise SystemExit(f"{name}: only interest_method: average_daily_balance is supported for cards")
        statement_day = pick_day("statement_day", DEFAULT_STATEMENT_DAY, "statement closing day")
        due_day = pick_day("due_day", DEFAULT_CARD_DUE_DAY, "payment due day")

        promo = raw.get("promo") or {}
        promo_end = parse_date(promo.get("end_date"), f"{name}.promo.end_date")
        promo_apr = float(promo.get("apr", apr))

        def card_rate(day, promo_end=promo_end, promo_apr=promo_apr, apr=apr):
            active = promo_end is not None and day <= promo_end
            return (promo_apr if active else apr) / 100 / 365

        initial = {"balance": float(raw["balance"])}
        rates = {"balance": card_rate}

        deferred = raw.get("deferred_interest_promos") or []
        if len(deferred) > 1:
            raise SystemExit(f"{name}: only one deferred-interest promo per card is supported")

        first_statement_month = add_months(as_of.year, as_of.month, -1)
        all_statements = [month_day(y, m, statement_day)
                          for y, m in months_between(date(*first_statement_month, 1), end)]

        def due_after(statement: date) -> date:
            for k in range(3):
                year, month = add_months(statement.year, statement.month, k)
                due = month_day(year, month, due_day)
                if (due - statement).days >= MIN_STATEMENT_TO_DUE_DAYS:
                    return due
            raise AssertionError("unreachable")

        cycles = []
        issued = [s for s in all_statements if s <= as_of]
        total = sum(initial.values()) + sum(float(p["balance"]) for p in deferred)
        if issued and due_after(issued[-1]) > as_of:
            known = dict(initial)
            if deferred:
                known["promo"] = float(deferred[0]["balance"])
            cycles.append(Cycle(opens=issued[-1], due=due_after(issued[-1]),
                                known_minimum=min(current_minimum, total), known_balance=known))
        statements = [s for s in all_statements if as_of < s <= end]
        cycles += [Cycle(opens=s, due=due_after(s), statement=s)
                   for s in statements if due_after(s) <= end]

        debt = Debt(name, kind, initial, rates, statements, cycles, rule)

        if deferred:
            promo_cfg = deferred[0]
            debt.initial["promo"] = float(promo_cfg["balance"])
            debt.daily_rate["promo"] = lambda _day: 0.0
            debt.promo_bucket = "promo"
            debt.promo_expiration = parse_date(promo_cfg["expiration_date"], f"{name} promo expiration")
            debt.promo_pay_off_by = parse_date(promo_cfg.get("pay_off_by"), f"{name} promo pay_off_by") \
                or debt.promo_expiration - ONE_DAY
            before_expiry = [s for s in all_statements if s <= debt.promo_expiration]
            if len(before_expiry) >= 3:
                debt.promo_excess_from = before_expiry[-3]
            if raw.get("payment_allocation", "card_act_default") != "card_act_default":
                raise SystemExit(f"{name}: only payment_allocation: card_act_default is supported")

            original = promo_cfg.get("original_amount")
            purchased = parse_date(promo_cfg.get("purchase_date"), f"{name} promo purchase_date")
            if original is not None and purchased is not None:
                days = (debt.promo_expiration - purchased).days
                debt.missed_promo_cost = float(original) * apr / 100 * days / 365
            else:
                notes.assume(f"{name}: promo original_amount / purchase_date missing, so the cost of "
                             f"missing the promo deadline can't be estimated.")
    else:
        raise SystemExit(f"{name}: unknown debt kind {kind!r}")

    # A due date whose payment would have to be sent before the plan starts is
    # assumed to be handled already.
    kept = []
    for cycle in debt.cycles:
        if bd.subtract(cycle.due, lag) < start:
            notes.assume(f"{name}: the payment due {cycle.due} would have to be sent before the plan "
                         f"starts ({start}); assumed you're paying it separately.", affects_dates=True)
        else:
            kept.append(cycle)
    debt.cycles = kept
    return debt


# ============================================================
# Linear model helpers
# ============================================================

class Lin:
    """Linear expression: sum(coefficient * variable) + constant."""

    __slots__ = ("terms", "const")

    def __init__(self, terms=None, const=0.0):
        self.terms = dict(terms or {})
        self.const = float(const)

    def __add__(self, other):
        if isinstance(other, Lin):
            terms = dict(self.terms)
            for var, coef in other.terms.items():
                terms[var] = terms.get(var, 0.0) + coef
            return Lin(terms, self.const + other.const)
        return Lin(self.terms, self.const + other)

    __radd__ = __add__

    def __mul__(self, k):
        return Lin({var: coef * k for var, coef in self.terms.items()}, self.const * k)

    __rmul__ = __mul__

    def __neg__(self):
        return self * -1

    def __sub__(self, other):
        return self + (-other)

    def __rsub__(self, other):
        return (-self) + other


@contextmanager
def quiet_stdout():
    """Hide HiGHS's C-level debug prints."""
    sys.stdout.flush()
    saved, devnull = os.dup(1), os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    try:
        yield
    finally:
        ctypes.CDLL(None).fflush(None)   # flush C's buffer while it still points at devnull
        os.dup2(saved, 1)
        os.close(devnull)
        os.close(saved)


class Model:
    def __init__(self):
        self.lb, self.ub, self.cost, self.integer = [], [], [], []
        self.rows, self.lo, self.hi = [], [], []

    def var(self, lb=0.0, ub=np.inf, cost=0.0, integer=False) -> Lin:
        self.lb.append(lb)
        self.ub.append(ub)
        self.cost.append(cost)
        self.integer.append(1 if integer else 0)
        return Lin({len(self.lb) - 1: 1.0})

    def binary(self) -> Lin:
        return self.var(0, 1, integer=True)

    def constrain(self, expr: Lin, lo=-np.inf, hi=np.inf):
        if not expr.terms:
            if not lo - 1e-9 <= expr.const <= hi + 1e-9:
                raise ValueError("constant constraint can't be satisfied")
            return
        self.rows.append(expr.terms)
        self.lo.append(lo - expr.const)
        self.hi.append(hi - expr.const)

    def eq(self, expr: Lin):
        self.constrain(expr, 0, 0)

    def nonneg(self, expr: Lin):
        self.constrain(expr, 0)

    def minimize(self, expr: Lin):
        for var, coef in expr.terms.items():
            self.cost[var] += coef

    def solve(self, time_limit=120):
        r, c, v = [], [], []
        for i, terms in enumerate(self.rows):
            for var, coef in terms.items():
                r.append(i)
                c.append(var)
                v.append(coef)
        matrix = coo_matrix((v, (r, c)), shape=(len(self.rows), len(self.lb))).tocsr()
        with quiet_stdout():
            result = milp(
                c=np.array(self.cost),
                integrality=np.array(self.integer),
                bounds=Bounds(np.array(self.lb), np.array(self.ub)),
                constraints=LinearConstraint(matrix, np.array(self.lo), np.array(self.hi)),
                options={"time_limit": time_limit, "mip_rel_gap": 1e-7},
            )
        if result.x is None:
            raise SystemExit(f"Optimization failed: {result.message}")
        if result.status != 0:
            print(f"WARNING: solver stopped early ({result.message}); plan may not be optimal.")
        return result.x

    @staticmethod
    def value(x, expr: Lin) -> float:
        return expr.const + sum(coef * x[var] for var, coef in expr.terms.items())


# ============================================================
# Optimizer
# ============================================================

@dataclass
class PlannedPayment:
    debt: str
    send: date
    post: date
    amount: float
    # Filled in by the simulation.
    balance_after: float | None = None
    cycle: dict | None = None
    payoff: bool = False


def payment_options(inp: Inputs, debt: Debt) -> list[tuple[date, date]]:
    """(send date, post date) pairs: every payday, plus in time for each due date."""
    options = set()
    for payday in inp.paydays:
        if payday >= inp.start:
            post = inp.bd.add(payday, inp.lag)
            if post <= inp.end:
                options.add((payday, post))
    for cycle in debt.cycles:
        options.add((inp.bd.subtract(cycle.due, inp.lag), cycle.due))
    return sorted(options)


def fixed_minimum(rule: dict, statement: date) -> float | None:
    """The minimum if it doesn't depend on the balance, else None."""
    if rule["type"] == "fixed":
        return rule["amount"]
    if rule.get("fixed_until") and statement <= rule["fixed_until"]:
        return rule["fixed_amount"]
    return None


def minimum_pieces(rule: dict, statement: date, statement_balance: Lin, interest: Lin) -> list[Lin]:
    """The minimum is the largest of these (then capped at the balance)."""
    fixed = fixed_minimum(rule, statement)
    if fixed is not None:
        return [Lin(const=fixed)]
    share = rule["percent"] / 100 * statement_balance
    if rule["type"] == "percent_plus_interest":
        share = share + interest
    return [share, Lin(const=rule["floor"])]


def optimize(inp: Inputs):
    model = Model()
    interest_by_debt: dict[str, Lin] = {}
    sends: list[tuple[date, Lin]] = []
    plans: list[tuple[Debt, date, date, dict[str, Lin]]] = []

    for debt in inp.debts:
        buckets = list(debt.initial)
        options = []
        for send, post in payment_options(inp, debt):
            parts = {b: model.var(cost=EARLY_TIEBREAK * (send - inp.start).days) for b in buckets}
            options.append((send, post, parts))
            sends.append((send, sum(parts.values(), Lin())))
            plans.append((debt, send, post, parts))

        events = {inp.as_of, inp.end, *debt.statements, *(c.due for c in debt.cycles),
                  *(post for _, post, _ in options)}
        if debt.promo_pay_off_by:
            events.add(debt.promo_pay_off_by)
        events = sorted(e for e in events if inp.as_of <= e <= inp.end)
        index = {e: i for i, e in enumerate(events)}
        statement_days = set(debt.statements)

        paid_on = defaultdict(list)
        for _, post, parts in options:
            paid_on[post].append(parts)

        # balance[b][i] = balance after everything that happens on events[i].
        # It holds for every day until the next event.
        balance = {b: [Lin(const=debt.initial[b])] for b in buckets}
        accrued = {b: Lin() for b in buckets}
        billed: dict[int, Lin] = {}
        interest = Lin()

        for i in range(1, len(events)):
            for b in buckets:
                k = sum(debt.daily_rate[b](d) for d in days_between(events[i - 1], events[i]))
                if k:
                    accrued[b] = accrued[b] + k * balance[b][i - 1]
                    interest = interest + k * balance[b][i - 1]

            # Loan interest is settled by every payment; card interest is
            # added to the balance when the statement closes.
            settle = (debt.kind == "installment_loan" or events[i] in statement_days
                      or events[i] == inp.end)
            if settle:
                billed[i] = sum(accrued.values(), Lin())

            for b in buckets:
                expr = balance[b][i - 1] - sum((p[b] for p in paid_on[events[i]]), Lin())
                if settle:
                    expr = expr + accrued[b]
                new = model.var(ub=BIG_M)
                model.eq(new - expr)
                balance[b].append(new)
            if settle:
                accrued = {b: Lin() for b in buckets}

        interest_by_debt[debt.name] = interest
        for b in buckets:
            model.eq(balance[b][-1])

        if debt.promo_bucket:
            model.eq(balance[debt.promo_bucket][index[debt.promo_pay_off_by]])

        def paid_in(cycle, bucket=None):
            total = Lin()
            for _, post, parts in options:
                if cycle.opens < post <= cycle.due:
                    total = total + (parts[bucket] if bucket else sum(parts.values(), Lin()))
            return total

        for cycle in debt.cycles:
            paid = paid_in(cycle)
            either = model.binary()   # 1 = pays the full balance instead of the minimum
            if debt.kind == "installment_loan":
                pieces = [Lin(const=debt.minimum["amount"])]
                owed_after = sum((balance[b][index[cycle.due]] for b in buckets), Lin())
                model.nonneg(BIG_M - BIG_M * either - owed_after)
                promo_owed = None
            elif cycle.known_minimum is not None:
                pieces = [Lin(const=cycle.known_minimum)]
                statement_balance = Lin(const=sum(cycle.known_balance.values()))
                model.nonneg(paid - statement_balance + BIG_M * (1 - either))
                promo_owed = Lin(const=cycle.known_balance.get("promo", 0.0))
            else:
                at = index[cycle.statement]
                statement_balance = sum((balance[b][at] for b in buckets), Lin())
                pieces = minimum_pieces(debt.minimum, cycle.statement, statement_balance,
                                        billed.get(at, Lin()))
                model.nonneg(paid - statement_balance + BIG_M * (1 - either))
                promo_owed = balance[debt.promo_bucket][at] if debt.promo_bucket else None
            for piece in pieces:
                model.nonneg(paid - piece + BIG_M * either)

            # Best Buy (CARD Act): the minimum goes to the lowest-APR balance
            # (the promo) first, so the promo receives at least the minimum,
            # or its whole balance.
            if debt.promo_bucket and promo_owed is not None and cycle.due <= debt.promo_pay_off_by:
                promo_paid = paid_in(cycle, debt.promo_bucket)
                clears = model.binary()
                for piece in pieces:
                    model.nonneg(promo_paid - piece + BIG_M * clears)
                model.nonneg(promo_paid - promo_owed + BIG_M * (1 - clears))

    # ---- Checking account
    fixed = defaultdict(float)
    for event in inp.cash_events:
        fixed[event.day] += event.amount
    sent_on = defaultdict(list)
    for day, amount in sends:
        sent_on[day].append(amount)

    shortfall: dict[date, Lin] = {}
    checking = Lin(const=inp.checking)
    for day in sorted(set(fixed) | set(sent_on)):
        extra = model.var(cost=SHORTFALL_PENALTY)
        shortfall[day] = extra
        new = model.var(lb=inp.buffer + CASH_MARGIN)
        model.eq(new - (checking + fixed[day] - sum(sent_on[day], Lin()) + extra))
        checking = new

    if inp.monthly_cap is not None:
        by_month = defaultdict(Lin)
        for day, amount in sends:
            by_month[(day.year, day.month)] = by_month[(day.year, day.month)] + amount
        for total in by_month.values():
            model.nonneg(float(inp.monthly_cap) - total)

    model.minimize(sum(interest_by_debt.values(), Lin()))
    x = model.solve()

    payments = defaultdict(float)
    for debt, send, post, parts in plans:
        amount = sum(Model.value(x, p) for p in parts.values())
        if amount > 0.005:
            payments[(debt.name, send, post)] += amount
    planned = [PlannedPayment(name, send, post, round(amount, 2))
               for (name, send, post), amount in sorted(payments.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    planned = [p for p in planned if p.amount >= 0.01]

    extra_cash = {day: round(Model.value(x, e), 2) for day, e in shortfall.items()}
    extra_cash = {day: amount for day, amount in extra_cash.items() if amount >= 0.01}
    return planned, {name: Model.value(x, e) for name, e in interest_by_debt.items()}, extra_cash


# ============================================================
# Independent day-by-day simulation
# ============================================================

@dataclass
class SimResult:
    interest: dict[str, float]
    paid_off: dict[str, date | None]
    final_owed: dict[str, float]
    problems: list[str]
    low_cash: list[tuple[date, float]]     # days checking is below the buffer
    cycles: dict[str, list[dict]]


def simulate(inp: Inputs, payments: list[PlannedPayment], extra_cash: dict[date, float],
             settle_payoffs: bool = False) -> SimResult:
    """Replay the payments with the lender/card rules, one day at a time.

    With settle_payoffs, a payment that would leave less than $1 owed (or
    overpay) becomes the exact payoff amount, and later payments to that debt
    drop to $0. The optimizer's balances differ from the lender's by cents,
    so this is what "pay the payoff amount" means in practice.
    """
    posting = defaultdict(list)
    sent = defaultdict(float)
    for p in payments:
        posting[(p.debt, p.post)].append(p)
        sent[p.send] += p.amount
    fixed = defaultdict(float)
    for event in inp.cash_events:
        fixed[event.day] += event.amount
    for day, amount in extra_cash.items():
        fixed[day] += amount

    balances = {d.name: dict(d.initial) for d in inp.debts}
    accrued = {d.name: {b: 0.0 for b in d.initial} for d in inp.debts}
    interest = {d.name: 0.0 for d in inp.debts}
    last_owed = {d.name: inp.as_of for d in inp.debts}
    cycles = {
        d.name: [{"opens": c.opens, "due": c.due, "statement": c.statement,
                  "minimum": (c.known_minimum if c.known_minimum is not None
                              else d.minimum["amount"] if d.kind == "installment_loan" else None),
                  "paid": 0.0} for c in d.cycles]
        for d in inp.debts
    }
    problems: list[str] = []
    low_cash: list[tuple[date, float]] = []
    checking = inp.checking

    def open_cycle(name, day):
        for cycle in cycles[name]:
            if cycle["opens"] < day <= cycle["due"]:
                return cycle
        return None

    def apply(debt: Debt, amount: float, cycle, day):
        bal, acc = balances[debt.name], accrued[debt.name]
        if debt.kind == "installment_loan":
            to_interest = min(amount, acc["principal"])
            acc["principal"] -= to_interest
            bal["principal"] -= amount - to_interest
            return
        if not debt.promo_bucket:
            bal["balance"] -= amount
            absorb_credit(bal, acc)
            return
        # CARD Act allocation.
        owed_min = max(0.0, (cycle["minimum"] or 0.0) - cycle["paid"]) if cycle else 0.0
        to_minimum = min(amount, owed_min)
        late_promo = (debt.promo_excess_from is not None
                      and debt.promo_excess_from < day <= debt.promo_expiration)
        for portion, order in ((to_minimum, ["promo", "balance"]),
                               (amount - to_minimum,
                                ["promo", "balance"] if late_promo else ["balance", "promo"])):
            for bucket in order:
                take = min(portion, max(bal[bucket], 0.0))
                bal[bucket] -= take
                portion -= take
            bal[order[-1]] -= portion
        absorb_credit(bal, acc)

    def absorb_credit(bal, acc):
        # A payoff amount includes interest accrued since the statement.
        for bucket in bal:
            for other in acc:
                take = min(max(-bal[bucket], 0.0), acc[other])
                acc[other] -= take
                bal[bucket] += take

    day = inp.as_of
    while day <= inp.end:
        for debt in inp.debts:
            name = debt.name
            for p in posting.get((name, day), []):
                owed = sum(balances[name].values()) + sum(accrued[name].values())
                if settle_payoffs and (p.amount > owed or owed - p.amount < 1.00):
                    p.amount = math.ceil(owed * 100 - 1e-6) / 100 if owed > 0.005 else 0.0
                    p.payoff = p.amount > 0
                cycle = open_cycle(name, day)
                apply(debt, p.amount, cycle, day)
                if cycle:
                    cycle["paid"] += p.amount
                    p.cycle = cycle
                p.balance_after = sum(balances[name].values()) + sum(accrued[name].values())

            if debt.kind == "credit_card" and day in debt.statements:
                charged = sum(accrued[name].values())
                for bucket in balances[name]:
                    balances[name][bucket] += accrued[name][bucket]
                    accrued[name][bucket] = 0.0
                statement_balance = sum(balances[name].values())
                rule = debt.minimum
                minimum = fixed_minimum(rule, day)
                if minimum is None:
                    share = rule["percent"] / 100 * statement_balance
                    if rule["type"] == "percent_plus_interest":
                        share += charged
                    minimum = max(rule["floor"], share)
                for cycle in cycles[name]:
                    if cycle["statement"] == day:
                        cycle["minimum"] = min(minimum, max(statement_balance, 0.0))
                        cycle["balance"] = statement_balance

            for bucket, bal in balances[name].items():
                daily = bal * debt.daily_rate[bucket](day)
                accrued[name][bucket] += daily
                interest[name] += daily

            owed = sum(balances[name].values()) + sum(accrued[name].values())
            if owed > 0.01:
                last_owed[name] = day
            if min(balances[name].values()) < -0.05 and not any("overpaid" in x and name in x for x in problems):
                problems.append(f"{name}: overpaid (balance {min(balances[name].values()):,.2f}) on {day}")

            for cycle in cycles[name]:
                if cycle["due"] == day and cycle["minimum"] is not None:
                    if debt.kind == "installment_loan" and owed <= 0.01:
                        continue
                    if cycle["paid"] < cycle["minimum"] - 0.01:
                        problems.append(f"{name}: paid ${cycle['paid']:,.2f} toward the "
                                        f"${cycle['minimum']:,.2f} minimum due {day}")

            if debt.promo_bucket and day == debt.promo_pay_off_by \
                    and balances[name][debt.promo_bucket] > 0.01:
                problems.append(f"{name}: promo balance ${balances[name][debt.promo_bucket]:,.2f} "
                                f"still owed on {day} (deadline)")

        if not settle_payoffs:
            checking += fixed.get(day, 0.0) - sent.get(day, 0.0)
        if day > inp.as_of and checking < inp.buffer - 0.005:
            low_cash.append((day, checking))
        day += ONE_DAY

    final_owed = {n: sum(balances[n].values()) + sum(accrued[n].values()) for n in balances}
    for name, owed in final_owed.items():
        if owed > 0.05:
            problems.append(f"{name}: ${owed:,.2f} still owed at plan end")
    paid_off = {n: (last_owed[n] + ONE_DAY if final_owed[n] <= 0.05 else None) for n in balances}
    return SimResult(interest, paid_off, final_owed, problems, low_cash, cycles)


# ============================================================
# Output
# ============================================================

def money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def ics_text(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def fold(line: str) -> str:
    """Fold a content line at 75 bytes (RFC 5545)."""
    data = line.encode()
    parts = []
    while len(data) > 75:
        cut = 75
        while data[cut] & 0xC0 == 0x80:   # don't split a UTF-8 character
            cut -= 1
        parts.append(data[:cut].decode())
        data = b" " + data[cut:]
    parts.append(data.decode())
    return "\r\n".join(parts)


def calendar_events(inp: Inputs, payments: list[PlannedPayment], sim: SimResult) -> list[dict]:
    prefix = "[DRAFT] " if inp.notes.draft else ""
    events = []
    for p in payments:
        lines = [f"Send {money(p.amount)} to {p.debt} today. It should post by {p.post:%a %b %d}."]
        if p.cycle and p.cycle.get("minimum") is not None:
            lines.append(f"Counts toward the {money(p.cycle['minimum'])} minimum due "
                         f"{p.cycle['due']:%b %d}.")
        if p.payoff:
            lines.append(f"This should pay {p.debt} off. Pay the payoff amount the account shows "
                         f"(including interest since the last statement), even if it differs "
                         f"by a little.")
        elif p.balance_after is not None:
            lines.append(f"Projected {p.debt} balance after it posts: {money(p.balance_after)}.")
        lines.append("Projected from the payoff plan. Re-run the plan monthly with real balances.")
        events.append({"day": p.send, "summary": f"{prefix}Pay {p.debt} {money(p.amount)}",
                       "description": "\n".join(lines), "alarm": True})

    for name, day in sim.paid_off.items():
        if day:
            events.append({"day": day, "summary": f"{prefix}{name} paid off",
                           "description": f"{name} should be at $0 as of today.", "alarm": False})

    if inp.calendar.get("include_income", True):
        sent = defaultdict(float)
        for p in payments:
            sent[p.send] += p.amount
        for e in inp.cash_events:
            if e.kind == "income":
                note = f"Debt payments sent today: {money(sent[e.day])}." if sent[e.day] else \
                    "No debt payments today."
                events.append({"day": e.day, "summary": f"{prefix}Payday +{money(e.amount)}",
                               "description": note, "alarm": False})

    if inp.calendar.get("include_bills", False):
        for e in inp.cash_events:
            if e.kind in ("bill", "expense"):
                events.append({"day": e.day, "summary": f"{prefix}{e.name} {money(-e.amount)}",
                               "description": "Scheduled bill from payoff_config.yaml.",
                               "alarm": False})
    return sorted(events, key=lambda e: (e["day"], e["summary"]))


def write_ics(path: Path, inp: Inputs, events: list[dict]):
    reminder = inp.calendar.get("reminder_minutes_before")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//payoff_planner//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Debt payoff plan",
             f"X-WR-TIMEZONE:{inp.timezone}"]
    for e in events:
        uid = hashlib.sha1(f"{e['day']}|{e['summary']}".encode()).hexdigest()[:20]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@payoff-planner",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{e['day']:%Y%m%d}",
            f"DTEND;VALUE=DATE:{e['day'] + ONE_DAY:%Y%m%d}",
            f"SUMMARY:{ics_text(e['summary'])}",
            f"DESCRIPTION:{ics_text(e['description'])}",
            "TRANSP:TRANSPARENT",
        ]
        if e["alarm"] and reminder is not None:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{ics_text(e['summary'])}",
                      f"TRIGGER:-PT{int(reminder)}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(fold(line) for line in lines) + "\r\n", newline="")


def write_csv(path: Path, payments: list[PlannedPayment]):
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["send_date", "posts_by", "account", "amount", "balance_after",
                         "counts_toward_due_date", "minimum_due"])
        for p in payments:
            cycle = p.cycle or {}
            writer.writerow([p.send, p.post, p.debt, f"{p.amount:.2f}",
                             "" if p.balance_after is None else f"{max(p.balance_after, 0):.2f}",
                             cycle.get("due", ""),
                             "" if cycle.get("minimum") is None else f"{cycle['minimum']:.2f}"])


def print_report(inp, payments, lp_interest, extra_cash, sim, ics_path, csv_path, n_events):
    width = 72
    print("=" * width)
    print("DEBT PAYOFF PLAN" + ("  (DRAFT: some dates are guessed, see assumptions)" if inp.notes.draft else ""))
    print("=" * width)
    print(f"Plan: {inp.start} to {inp.end}, starting checking {money(inp.checking)} "
          f"on {inp.as_of}, buffer {money(inp.buffer)}")

    print(f"\n{'Debt':<16}{'Paid off':>12}{'Interest':>14}{'Payments':>14}")
    for debt in inp.debts:
        total_paid = sum(p.amount for p in payments if p.debt == debt.name)
        paid_off = sim.paid_off[debt.name]
        print(f"{debt.name:<16}{str(paid_off or 'NOT PAID'):>12}"
              f"{money(sim.interest[debt.name]):>14}{money(total_paid):>14}")
    print(f"{'Total':<16}{'':>12}{money(sum(sim.interest.values())):>14}"
          f"{money(sum(p.amount for p in payments)):>14}")

    for debt in inp.debts:
        if debt.missed_promo_cost:
            print(f"\n{debt.name} promo: missing the {debt.promo_expiration} deadline would add "
                  f"about {money(debt.missed_promo_cost)} of deferred interest.")

    print("\nCASH")
    if extra_cash:
        print(f"  ! Your cash flow can't cover these dates while keeping the {money(inp.buffer)} buffer.")
        print("    The plan assumes this extra money shows up (savings, a transfer, a smaller bill):")
        for day, amount in sorted(extra_cash.items()):
            names = ", ".join(e.name for e in inp.cash_events if e.day == day and e.amount < 0)
            print(f"      {day}: {money(amount)} more needed{f'  ({names} due)' if names else ''}")
        print(f"    Total: {money(sum(extra_cash.values()))}")
    else:
        print(f"  Checking never drops below the {money(inp.buffer)} buffer.")

    print("\nASSUMPTIONS")
    for note in inp.notes.assumptions or ["None."]:
        print(f"  - {note}")

    print("\nCHECK (independent day-by-day replay of the rounded schedule)")
    lp_total, sim_total = sum(lp_interest.values()), sum(sim.interest.values())
    print(f"  Optimizer interest {money(lp_total)} vs replay {money(sim_total)} "
          f"(difference {money(sim_total - lp_total)})")
    for day, bal in sim.low_cash[:5]:
        print(f"  ! checking {money(bal)} on {day}, below the buffer")
    if sim.problems:
        for problem in sim.problems:
            print(f"  ! {problem}")
    elif not sim.low_cash:
        print("  All minimums met, promo deadline met, buffer kept, every debt at $0 by the end.")

    print("\nNEXT 45 DAYS")
    horizon = inp.start + timedelta(days=45)
    upcoming = [p for p in payments if p.send <= horizon]
    for p in upcoming:
        print(f"  {p.send:%a %Y-%m-%d}  send {money(p.amount):>11} to {p.debt:<14} "
              f"(posts by {p.post:%b %d})")
    if not upcoming:
        print("  No payments.")

    print(f"\nWrote {ics_path.name} ({n_events} events) and {csv_path.name} ({len(payments)} payments).")


def main():
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else "payoff_config.yaml")
    cfg = yaml.safe_load(config_path.read_text())
    inp = load_inputs(cfg)

    payments, lp_interest, extra_cash = optimize(inp)
    payments = [p for p in payments if p.amount >= 1.00]
    simulate(inp, payments, extra_cash, settle_payoffs=True)
    payments = [p for p in payments if p.amount >= 0.01]
    sim = simulate(inp, payments, extra_cash)

    ics_path = config_path.parent / inp.calendar.get("output_file", "payoff_schedule.ics")
    csv_path = ics_path.with_suffix(".csv")
    events = calendar_events(inp, payments, sim)
    write_ics(ics_path, inp, events)
    write_csv(csv_path, payments)
    print_report(inp, payments, lp_interest, extra_cash, sim, ics_path, csv_path, len(events))


if __name__ == "__main__":
    main()
