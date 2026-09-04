"""
Multi-Module App Packages:
Each channel module (shopeefood, grabmart, shopee) is strictly decoupled and independent.
Modules implement BaseChannelAdapter and register to ChannelRegistry without overlapping.
"""
