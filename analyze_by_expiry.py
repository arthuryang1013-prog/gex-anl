"""
analyze_by_expiry.py -- breaks the saved snapshot (nq_gex_snapshot.csv) down by expiry.
No IBKR connection needed. Put this file in the same folder as nq_gex_snapshot.csv and run:
    python3 analyze_by_expiry.py
"""
import pandas as pd

pd.options.display.float_format = "{:,.1f}".format
pd.options.display.width = 200

df = pd.read_csv("nq_gex_snapshot.csv")
print(f"Contracts kept: {len(df)}   |   total net GEX: {df['gex'].sum():,.0f} $ per 1%\n")

# 1) Which expiries carry the exposure?
g = df.groupby(["expiry", "cls"]).agg(
    contracts=("gex", "size"),
    approx_underlying=("F", "median"),
    total_oi=("oi", "sum"),
    net_gex=("gex", "sum"),
    abs_gex=("gex", lambda s: s.abs().sum()),
)
g["share_of_abs_gex_%"] = g["abs_gex"] / g["abs_gex"].sum() * 100
print("=== By expiry / trading class ===")
print(g.drop(columns="abs_gex").to_string())

# 2) Walls per expiry (strike with the largest call GEX / most negative put GEX)
print("\n=== Walls per expiry ===")
for exp, sub in df.groupby("expiry"):
    calls = sub[sub["right"] == "C"].groupby("strike")["gex"].sum()
    puts = sub[sub["right"] == "P"].groupby("strike")["gex"].sum()
    cw = f"{calls.idxmax():,.0f}" if len(calls) else "n/a"
    pw = f"{puts.idxmin():,.0f}" if len(puts) else "n/a"
    print(f"{exp}: call wall {cw} | put wall {pw} | strikes kept {sub['strike'].nunique()} "
          f"({sub['strike'].min():,.0f} - {sub['strike'].max():,.0f})")

# 3) Coverage: calls vs puts kept per expiry (a big imbalance hints at missing data)
print("\n=== Calls / puts kept per expiry ===")
print(df.groupby(["expiry", "right"]).size().unstack(fill_value=0).to_string())

# 4) Net GEX if the nearest expiry is excluded
nearest = df["expiry"].min()
rest = df[df["expiry"] != nearest]
print(f"\nNet GEX excluding nearest expiry ({nearest}): {rest['gex'].sum():,.0f} $ per 1%")
