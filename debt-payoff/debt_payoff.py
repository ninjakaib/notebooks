from __future__ import annotations

from datetime import date
from typing import Dict, List

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


# ============================================================
# USER INPUTS / ASSUMPTIONS
# ============================================================

START_DATE = date(2026, 10, 1)
HORIZON_MONTHS = 36

MONTHLY_BUDGET = 1_000.00


# -------------------------
# Personal loan
# -------------------------

LOAN_INITIAL_BALANCE = 15_000.00
LOAN_APR = 0.1498
LOAN_MIN_PAYMENT = 356.70


# -------------------------
# Robinhood
# -------------------------

RH_INITIAL_BALANCE = 5_250.00

# Assumed end of the 0% period.
# Change this once you know the exact date.
RH_PROMO_END = (2027, 5)

# Using the original contractual APR discussed previously.
RH_APR_AFTER_PROMO = 0.2824

RH_MIN_PAYMENT = 54.00


# -------------------------
# Best Buy
# -------------------------

# Total balance ≈ $2,170
# $2,053.24 is the deferred-interest promotional purchase.
# The remainder is ordinary revolving balance.
BB_DEFERRED_BALANCE = 2_053.24
BB_NORMAL_BALANCE = 2_170.00 - BB_DEFERRED_BALANCE

BB_APR = 0.30
BB_MIN_PAYMENT = 30.00

# Promotion expires March 12, 2028.
#
# We require the deferred balance to be gone BEFORE March,
# i.e. at the end of February, to leave a safety margin.
BB_SAFE_PAYOFF = (2028, 2)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    """Return (year, month) after adding n months."""
    absolute_month = year * 12 + (month - 1) + n
    new_year = absolute_month // 12
    new_month = absolute_month % 12 + 1
    return new_year, new_month


def build_months(start: date, n: int) -> List[tuple[int, int]]:
    """Build [(year, month), ...] for n months."""
    result = []

    for i in range(n):
        result.append(add_months(start.year, start.month, i))

    return result


# ============================================================
# OPTIMIZER
# ============================================================

def solve_debt_plan():
    """
    Solve the debt-payment optimization problem.

    Debts modeled:

        0 = Personal loan
        1 = Robinhood
        2 = Best Buy normal-interest balance
        3 = Best Buy deferred-interest balance

    Objective:
        Minimize total interest paid.

    Constraints:
        - Total monthly payments <= MONTHLY_BUDGET
        - Contractual minimum payments are respected
        - A debt's minimum disappears once it is paid off
        - Robinhood remains at 0% through RH_PROMO_END
        - Best Buy deferred balance must be paid off by BB_SAFE_PAYOFF
        - All balances must be paid off within the horizon
    """

    months = build_months(START_DATE, HORIZON_MONTHS)
    T = len(months)

    # --------------------------------------------------------
    # Monthly interest rates
    # --------------------------------------------------------

    loan_monthly_rate = LOAN_APR / 12
    bb_monthly_rate = BB_APR / 12

    rh_monthly_rates = []

    for year, month in months:
        if (year, month) <= RH_PROMO_END:
            rh_monthly_rates.append(0.0)
        else:
            rh_monthly_rates.append(RH_APR_AFTER_PROMO / 12)

    # --------------------------------------------------------
    # Variable layout
    # --------------------------------------------------------
    #
    # B[d, t] = debt balance at beginning of month t
    #
    # P[d, t] = payment toward debt d during month t
    #
    # Y[d, t] = binary:
    #            debt is still active during month t
    #
    # Z[d, t] = binary:
    #            month t is the final payoff month
    #
    # For Best Buy, debt 2 and debt 3 share one active/final
    # state because they are on the same credit-card account.
    #

    num_debts = 4

    # Beginning balances:
    # 4 debts * (T + 1) balance points
    num_balance_vars = num_debts * (T + 1)

    # Payments:
    # 4 debts * T months
    payment_offset = num_balance_vars
    num_payment_vars = num_debts * T

    # Active-state binaries:
    # loan, RH, Best Buy
    active_offset = payment_offset + num_payment_vars
    num_active_vars = 3 * T

    # Final-payoff binaries:
    final_offset = active_offset + num_active_vars
    num_final_vars = 3 * T

    total_variables = (
        final_offset
        + num_final_vars
    )

    def balance_idx(debt: int, t: int) -> int:
        return debt * (T + 1) + t

    def payment_idx(debt: int, t: int) -> int:
        return payment_offset + debt * T + t

    def active_idx(account: int, t: int) -> int:
        # account:
        #   0 = loan
        #   1 = Robinhood
        #   2 = Best Buy
        return active_offset + account * T + t

    def final_idx(account: int, t: int) -> int:
        return final_offset + account * T + t

    # --------------------------------------------------------
    # Objective vector
    # --------------------------------------------------------

    objective = np.zeros(total_variables)

    # Interest is based on beginning-of-month balances:
    #
    # interest = beginning balance * monthly rate
    #
    # We minimize the sum of all such interest charges.

    for t in range(T):

        # Personal loan
        objective[balance_idx(0, t)] += loan_monthly_rate

        # Robinhood
        objective[balance_idx(1, t)] += rh_monthly_rates[t]

        # Best Buy normal balance
        objective[balance_idx(2, t)] += bb_monthly_rate

        # Best Buy deferred balance:
        # 0% while promo is successfully paid.
        #
        # No interest included here because the optimizer is
        # explicitly constrained to pay the promotional balance
        # by the deadline.

    # --------------------------------------------------------
    # Variable bounds
    # --------------------------------------------------------

    lower_bounds = np.zeros(total_variables)
    upper_bounds = np.full(total_variables, np.inf)

    BIG_M = 50_000.00

    # Balances
    upper_bounds[:num_balance_vars] = BIG_M

    # Payments cannot exceed the monthly budget individually
    upper_bounds[
        payment_offset:
        payment_offset + num_payment_vars
    ] = MONTHLY_BUDGET

    # Binary variables
    upper_bounds[
        active_offset:
        active_offset + num_active_vars
    ] = 1

    upper_bounds[
        final_offset:
        final_offset + num_final_vars
    ] = 1

    # --------------------------------------------------------
    # Initial balances
    # --------------------------------------------------------

    initial_balances = [
        LOAN_INITIAL_BALANCE,
        RH_INITIAL_BALANCE,
        BB_NORMAL_BALANCE,
        BB_DEFERRED_BALANCE,
    ]

    for debt, balance in enumerate(initial_balances):
        idx = balance_idx(debt, 0)
        lower_bounds[idx] = balance
        upper_bounds[idx] = balance

    # --------------------------------------------------------
    # Integer / binary declarations
    # --------------------------------------------------------

    integrality = np.zeros(total_variables)

    integrality[
        active_offset:
        active_offset + num_active_vars
    ] = 1

    integrality[
        final_offset:
        final_offset + num_final_vars
    ] = 1

    # --------------------------------------------------------
    # Linear constraints
    # --------------------------------------------------------

    rows = []
    lower = []
    upper = []

    def add_constraint(
        coefficients: Dict[int, float],
        minimum: float = -np.inf,
        maximum: float = np.inf,
    ):
        rows.append(coefficients)
        lower.append(minimum)
        upper.append(maximum)

    # --------------------------------------------------------
    # Debt balance equations
    # --------------------------------------------------------

    for t in range(T):

        # Personal loan:
        #
        # B[t+1] =
        #     B[t] * (1 + rate)
        #     - payment[t]
        #
        # Rearranged:
        #
        # B[t+1] - (1+r)B[t] + P[t] = 0

        add_constraint(
            {
                balance_idx(0, t + 1): 1,
                balance_idx(0, t): -(1 + loan_monthly_rate),
                payment_idx(0, t): 1,
            },
            0,
            0,
        )

        # Robinhood
        add_constraint(
            {
                balance_idx(1, t + 1): 1,
                balance_idx(1, t): -(1 + rh_monthly_rates[t]),
                payment_idx(1, t): 1,
            },
            0,
            0,
        )

        # Best Buy normal
        add_constraint(
            {
                balance_idx(2, t + 1): 1,
                balance_idx(2, t): -(1 + bb_monthly_rate),
                payment_idx(2, t): 1,
            },
            0,
            0,
        )

        # Best Buy deferred
        #
        # 0% interest while successfully paid by the
        # promotional deadline.
        add_constraint(
            {
                balance_idx(3, t + 1): 1,
                balance_idx(3, t): -1,
                payment_idx(3, t): 1,
            },
            0,
            0,
        )

    # --------------------------------------------------------
    # Monthly total budget
    # --------------------------------------------------------

    for t in range(T):

        add_constraint(
            {
                payment_idx(0, t): 1,
                payment_idx(1, t): 1,
                payment_idx(2, t): 1,
                payment_idx(3, t): 1,
            },
            -np.inf,
            MONTHLY_BUDGET,
        )

    # ========================================================
    # PERSONAL LOAN + ROBINHOOD ACCOUNT RULES
    # ========================================================

    for account, debt, minimum_payment in [
        (0, 0, LOAN_MIN_PAYMENT),
        (1, 1, RH_MIN_PAYMENT),
    ]:

        # They start active.
        add_constraint(
            {
                active_idx(account, 0): 1,
            },
            1,
            1,
        )

        for t in range(T):

            # Positive balance requires active state.
            #
            # B <= M * Y
            add_constraint(
                {
                    balance_idx(debt, t): 1,
                    active_idx(account, t): -BIG_M,
                },
                -np.inf,
                0,
            )

            # Payments can only occur while active.
            #
            # P <= M * Y
            add_constraint(
                {
                    payment_idx(debt, t): 1,
                    active_idx(account, t): -BIG_M,
                },
                -np.inf,
                0,
            )

            # Contractual minimum:
            #
            # P >= minimum * Y - M * Z
            #
            # Z allows the final payment to be smaller than
            # the normal contractual minimum.
            add_constraint(
                {
                    payment_idx(debt, t): 1,
                    active_idx(account, t): -minimum_payment,
                    final_idx(account, t): BIG_M,
                },
                0,
                np.inf,
            )

            # Final flag can only be set while active.
            #
            # Z <= Y
            add_constraint(
                {
                    final_idx(account, t): 1,
                    active_idx(account, t): -1,
                },
                -np.inf,
                0,
            )

            # If this is the final month, the account must
            # be inactive next month.
            if t < T - 1:
                add_constraint(
                    {
                        active_idx(account, t + 1): 1,
                        active_idx(account, t): -1,
                        final_idx(account, t): 1,
                    },
                    -np.inf,
                    0,
                )

        # A debt has at most one final payoff month.
        add_constraint(
            {
                final_idx(account, t): 1
                for t in range(T)
            },
            -np.inf,
            1,
        )

    # ========================================================
    # BEST BUY ACCOUNT RULES
    # ========================================================

    # Best Buy has two sub-balances but one monthly minimum.
    #
    # The optimizer can decide how the $30 is split between
    # the normal and deferred balances. Because the normal
    # balance accrues ~30%, the optimal solution naturally
    # attacks it before the 0% deferred balance.

    add_constraint(
        {
            active_idx(2, 0): 1,
        },
        1,
        1,
    )

    for t in range(T):

        # Either Best Buy sub-balance being positive means
        # the card account is active.

        add_constraint(
            {
                balance_idx(2, t): 1,
                active_idx(2, t): -BIG_M,
            },
            -np.inf,
            0,
        )

        add_constraint(
            {
                balance_idx(3, t): 1,
                active_idx(2, t): -BIG_M,
            },
            -np.inf,
            0,
        )

        # Payments can occur only while the account is active.

        add_constraint(
            {
                payment_idx(2, t): 1,
                active_idx(2, t): -BIG_M,
            },
            -np.inf,
            0,
        )

        add_constraint(
            {
                payment_idx(3, t): 1,
                active_idx(2, t): -BIG_M,
            },
            -np.inf,
            0,
        )

        # Combined Best Buy minimum payment.
        #
        # P_normal + P_deferred >=
        #     $30 * active
        #
        # Again, the final-payoff flag permits the final
        # payment to be below $30.
        add_constraint(
            {
                payment_idx(2, t): 1,
                payment_idx(3, t): 1,
                active_idx(2, t): -BB_MIN_PAYMENT,
                final_idx(2, t): BIG_M,
            },
            0,
            np.inf,
        )

        # Final flag requires active state.
        add_constraint(
            {
                final_idx(2, t): 1,
                active_idx(2, t): -1,
            },
            -np.inf,
            0,
        )

        if t < T - 1:

            # Once final payoff occurs, account becomes inactive.
            add_constraint(
                {
                    active_idx(2, t + 1): 1,
                    active_idx(2, t): -1,
                    final_idx(2, t): 1,
                },
                -np.inf,
                0,
            )

    # At most one final payoff month.
    add_constraint(
        {
            final_idx(2, t): 1
            for t in range(T)
        },
        -np.inf,
        1,
    )

    # ========================================================
    # REQUIRE EVERY DEBT TO BE GONE BY THE HORIZON
    # ========================================================

    for debt in range(4):

        add_constraint(
            {
                balance_idx(debt, T): 1,
            },
            0,
            0,
        )

    # ========================================================
    # BEST BUY DEFERRED-INTEREST DEADLINE
    # ========================================================
    #
    # We require the deferred promotional balance to be zero
    # at the START of March 2028.
    #
    # Therefore it must have been completely paid using
    # payments through February 2028.
    #

    deadline_index = next(
        i
        for i, (year, month) in enumerate(months)
        if (year, month) == BB_SAFE_PAYOFF
    ) + 1

    add_constraint(
        {
            balance_idx(3, deadline_index): 1,
        },
        0,
        0,
    )

    # ========================================================
    # SOLVE
    # ========================================================

    constraint_matrix = lil_matrix(
        (len(rows), total_variables)
    )

    for row_number, coefficients in enumerate(rows):
        for variable, coefficient in coefficients.items():
            constraint_matrix[row_number, variable] = coefficient

    constraint_matrix = constraint_matrix.tocsr()

    constraints = LinearConstraint(
        constraint_matrix,
        np.array(lower),
        np.array(upper),
    )

    result = milp(
        c=objective,
        integrality=integrality,
        bounds=Bounds(lower_bounds, upper_bounds),
        constraints=constraints,
        options={
            "time_limit": 30,
        },
    )

    if not result.success:
        raise RuntimeError(
            f"Optimization failed: {result.message}"
        )

    return result, months, {
        "balance_idx": balance_idx,
        "payment_idx": payment_idx,
        "active_idx": active_idx,
        "final_idx": final_idx,
        "T": T,
    }


# ============================================================
# REPORTING
# ============================================================

def extract_solution(
    result,
    months,
    indices,
):
    """Convert optimizer output into a readable DataFrame."""

    import pandas as pd

    balance_idx = indices["balance_idx"]
    payment_idx = indices["payment_idx"]
    T = indices["T"]

    rows = []

    for t, (year, month) in enumerate(months):

        loan_balance = result.x[balance_idx(0, t)]
        rh_balance = result.x[balance_idx(1, t)]
        bb_normal_balance = result.x[balance_idx(2, t)]
        bb_deferred_balance = result.x[balance_idx(3, t)]

        loan_payment = result.x[payment_idx(0, t)]
        rh_payment = result.x[payment_idx(1, t)]
        bb_normal_payment = result.x[payment_idx(2, t)]
        bb_deferred_payment = result.x[payment_idx(3, t)]

        if (year, month) <= RH_PROMO_END:
            rh_rate = 0.0
        else:
            rh_rate = RH_APR_AFTER_PROMO / 12

        loan_interest = (
            loan_balance
            * LOAN_APR
            / 12
        )

        rh_interest = (
            rh_balance
            * rh_rate
        )

        bb_normal_interest = (
            bb_normal_balance
            * BB_APR
            / 12
        )

        total_interest = (
            loan_interest
            + rh_interest
            + bb_normal_interest
        )

        rows.append(
            {
                "month": f"{year}-{month:02d}",

                "loan_begin": loan_balance,
                "loan_payment": loan_payment,

                "rh_begin": rh_balance,
                "rh_payment": rh_payment,

                "bb_normal_begin": bb_normal_balance,
                "bb_normal_payment": bb_normal_payment,

                "bb_deferred_begin": bb_deferred_balance,
                "bb_deferred_payment": bb_deferred_payment,

                "total_payment": (
                    loan_payment
                    + rh_payment
                    + bb_normal_payment
                    + bb_deferred_payment
                ),

                "loan_interest": loan_interest,
                "rh_interest": rh_interest,
                "bb_normal_interest": bb_normal_interest,

                "total_interest": total_interest,
            }
        )

    return pd.DataFrame(rows)


def print_summary(result, schedule):
    """Print the important optimization results."""

    print("\n" + "=" * 70)
    print("OPTIMAL DEBT PAYOFF PLAN")
    print("=" * 70)

    print(
        f"\nMinimum modeled interest: "
        f"${result.fun:,.2f}"
    )

    print("\nInterest by debt:")

    print(
        f"  Personal loan: "
        f"${schedule['loan_interest'].sum():,.2f}"
    )

    print(
        f"  Robinhood: "
        f"${schedule['rh_interest'].sum():,.2f}"
    )

    print(
        f"  Best Buy normal: "
        f"${schedule['bb_normal_interest'].sum():,.2f}"
    )

    print(
        "  Best Buy deferred: "
        "$0.00"
    )

    print(
        f"\nTotal interest: "
        f"${schedule['total_interest'].sum():,.2f}"
    )

    print("\nPayoff schedule:")

    # Determine when each debt reaches zero.

    for debt_name, balance_column, payment_column in [
        (
            "Personal loan",
            "loan_begin",
            "loan_payment",
        ),
        (
            "Robinhood",
            "rh_begin",
            "rh_payment",
        ),
        (
            "Best Buy normal",
            "bb_normal_begin",
            "bb_normal_payment",
        ),
        (
            "Best Buy deferred",
            "bb_deferred_begin",
            "bb_deferred_payment",
        ),
    ]:

        payoff_month = None

        for i in range(len(schedule)):

            beginning = schedule.loc[i, balance_column]
            payment = schedule.loc[i, payment_column]

            if (
                beginning > 0.005
                and payment >= beginning - 0.01
            ):
                payoff_month = schedule.loc[i, "month"]
                break

        print(
            f"  {debt_name}: "
            f"{payoff_month or 'not paid within horizon'}"
        )


def main():

    result, months, indices = solve_debt_plan()

    schedule = extract_solution(
        result,
        months,
        indices,
    )

    # Print only months in which there is debt.
    active_schedule = schedule[
        schedule["total_payment"] > 0.01
    ].copy()

    # Round output for readability.
    money_columns = [
        column
        for column in active_schedule.columns
        if column != "month"
    ]

    active_schedule[money_columns] = (
        active_schedule[money_columns].round(2)
    )

    print_summary(result, active_schedule)

    print("\nDetailed monthly schedule:")

    print(
        active_schedule[
            [
                "month",
                "loan_payment",
                "rh_payment",
                "bb_normal_payment",
                "bb_deferred_payment",
                "total_payment",
                "total_interest",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()