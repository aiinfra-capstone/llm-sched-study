"""Week-2 calibration campaign: the cost model, tau, and the synthesizable R range.

Three deliverables, one campaign:

  cost_model.py    C-3 snapshots — a time-ordered series, not one fitted model (F-6, F-7)
  stationarity.py  tau and the variance envelope — MPR-1, the result that stands alone
  rrange.py        how far apart F-9a can actually push two node classes (F-9a, MPR-2)

Nothing here imports the scheduler. That is the point of MPR-1: it is measured on
hardware with no policy in the loop, so it survives every way the rest of the study can
go wrong.
"""
