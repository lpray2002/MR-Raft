"""
Cluster Manager (CM) end-to-end pipeline for MR-Raft.

Modules
-------
client      gRPC wrapper around the Go ``mec-node-cm`` binary (data + control plane)
dynamics    time-varying coverage model -> spatial coverage graph G(t) + edge events
planner     region diff -> ordered CM directives (carrier matching + state handoff)
cm          the CM loop: every T_sync compute the net diff, repartition, dispatch
uav_workload UAV fleet issuing writes/linearizable reads through their serving node
metrics     responsiveness / reconfiguration overhead / consistency metrics
visualize   timeline figure, T_trans decomposition, topology animation
runner      one-shot orchestration (launch nodes, run, collect, plot)
"""
