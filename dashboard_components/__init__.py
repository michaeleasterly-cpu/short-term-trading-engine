"""Dashboard components — chart adapters and shared rendering helpers.

Per the operator-dashboard spec, all chart rendering routes through this
package so the underlying chart library (currently
``streamlit-lightweight-charts-pro``) is a 1-file swap to Plotly if its
0.x maintenance becomes a problem.
"""
