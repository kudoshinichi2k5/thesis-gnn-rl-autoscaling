# Mathematical Workload Dataset for Kubernetes Microservice Autoscaling

## Overview

This dataset contains telemetry collected from a real Kubernetes cluster while executing mathematically generated workloads on the **Online Boutique** microservice application. The purpose of the dataset is to evaluate and develop predictive autoscaling methods, workload forecasting models, graph neural networks (GNNs), and reinforcement learning (RL)-based autoscaling policies.

Unlike conventional load-testing datasets that replay simple fixed scenarios, this dataset generates workloads from stochastic mathematical models and records the complete system behavior under Kubernetes Horizontal Pod Autoscaler (HPA).

The dataset includes:

- Resource utilization of every microservice
- Replica changes produced by HPA
- Service-to-service communication graph
- Request latency and error statistics
- Complete workload metadata and execution traces

---

# Dataset Characteristics

| Property | Value |
|----------|------|
| Application | Online Boutique |
| Deployment Platform | Kubernetes |
| Autoscaler | Horizontal Pod Autoscaler (HPA) |
| Telemetry Sources | Prometheus + Jaeger |
| Load Generator | Open Arrival Locust Driver |
| Arrival Models | NHPP, MMPP, Bounded Pareto, ON/OFF |
| Number of Scenarios | 4 |
| Runs per Scenario | **8** |
| Total Runs | **32** |
| Default Run Duration | 30 minutes |
| Sampling Interval | 10 seconds |

Every scenario contains **8 independent runs** generated using different random seeds. Before each run, the script waits until all deployments are scaled back to one replica, ensuring that every experiment starts from a consistent initial state.

---

# Dataset Structure

```text
results/
│
├── nhpp/
│   ├── run_01/
│   ├── run_02/
│   ├── ...
│   └── run_08/
│
├── mmpp/
│   ├── run_01/
│   ├── ...
│   └── run_08/
│
├── pareto/
│   ├── run_01/
│   ├── ...
│   └── run_08/
│
└── onoff/
    ├── run_01/
    ├── ...
    └── run_08/
```

Each run contains all telemetry collected during one complete experiment.

---

# Workload Generation

Instead of using Locust's default closed-loop model, this project implements an **open arrival model**.

In a closed-loop workload, a virtual user waits until the previous request finishes before sending the next one. Consequently, the offered load decreases whenever the system becomes slow.

In contrast, the open arrival model schedules every request according to a pre-generated arrival trace. New sessions begin exactly at their scheduled timestamps regardless of previous response times. Therefore, the offered load is independent of system performance, making the generated workload consistent with queueing theory and stochastic arrival processes.

The arrival trace is generated from one of four mathematical models described below.

---

# Scenario 1 — Non-Homogeneous Poisson Process (NHPP)

## Purpose

The NHPP scenario models workloads with predictable temporal trends and seasonality. It is primarily designed for evaluating predictive autoscaling algorithms.

## Description

Session arrivals follow a Poisson process whose arrival rate changes continuously over time.

The arrival rate is represented as

$$
\lambda(t)
=
\text{base}
+
A_1\sin\left(\frac{2\pi t}{T}+\phi\right)
+
A_2\sin\left(\frac{4\pi t}{T}+2\phi\right)
$$

where two sinusoidal components create a smooth daily-like traffic pattern.

For the experiments, one complete cycle is compressed into **15 minutes**, allowing two complete traffic cycles during a 30-minute run.

Characteristics:

- Gradual increase in traffic
- Smooth peak
- Gradual decrease
- Repeating seasonal behavior

Typical frontend workload:

- approximately 3–80 requests/s
- average around 42 requests/s

Suitable for:

- predictive autoscaling
- workload forecasting
- trend prediction
- temporal machine learning models

---

# Scenario 2 — Markov Modulated Poisson Process (MMPP)

## Purpose

The MMPP scenario models sudden traffic changes caused by events such as flash sales or viral content.

## Description

A hidden continuous-time Markov chain switches among three traffic states:

- Idle
- Normal
- High

Each state has:

- its own Poisson arrival rate
- exponentially distributed residence time

Transitions occur randomly according to predefined transition probabilities.

Typical frontend traffic:

| State | Approximate Load |
|--------|-----------------|
| Idle | ~9 requests/s |
| Normal | ~21 requests/s |
| High | ~90 requests/s |

Unlike deterministic spike tests, both the timing and duration of traffic bursts are random.

Suitable for evaluating:

- reactive autoscaling
- HPA responsiveness
- burst detection
- state transition detection

---

# Scenario 3 — Bounded Pareto Workload

## Purpose

This scenario models heterogeneous request complexity rather than varying arrival rates.

## Description

Session arrivals remain approximately constant.

Instead, each shopping session contains a random number of purchased items following a bounded Pareto distribution.

Most sessions purchase only one to three products, while a small fraction of sessions purchase dozens of products.

As a result:

- request count remains relatively stable
- computational workload varies dramatically

Heavy shopping sessions generate significantly higher CPU consumption in:

- checkoutservice
- cartservice
- productcatalogservice
- currencyservice

This reproduces the well-known "80/20" heavy-tail effect commonly observed in production systems.

Suitable for evaluating:

- CPU-based autoscaling
- resource saturation
- workload heterogeneity
- heavy-tailed service demand

---

# Scenario 4 — ON/OFF Self-Similar Traffic

## Purpose

The ON/OFF scenario models realistic Internet traffic generated by many independent users.

## Description

The workload consists of multiple persistent clients.

Each client alternates between two states:

- ON
- OFF

During the ON period, requests are generated according to a Poisson process.

During the OFF period, the client remains idle.

Both ON and OFF durations follow bounded Pareto distributions.

When many independent sources are aggregated, the resulting traffic becomes self-similar with long-range dependence.

Characteristics:

- burstiness at multiple time scales
- realistic client behavior
- persistent user sessions
- highly variable traffic intensity

Suitable for evaluating:

- bursty workloads
- scaling oscillation
- long-range dependence
- service mesh communication behavior

---

# Files Generated for Each Run

Each run contains several metadata files together with three primary telemetry datasets.

```text
run_xx/
│
├── node_metrics.csv
├── service_metrics.csv
├── edge_metrics.csv
├── meta.json
├── driver_report.json
├── collect_report.json
├── locust_stats.csv
├── locust_failures.csv
├── locust_history.csv
├── locust.html
└── trace/
```

---

# Dataset 1 — node_metrics.csv

This dataset is collected from **Prometheus**.

Each record represents one microservice at one timestamp.

It contains infrastructure-level metrics including:

- CPU utilization
- Istio sidecar CPU utilization
- CPU throttling ratio
- Memory usage
- Network receive throughput
- Network transmit throughput
- CPU request
- CPU limit
- Restart count
- Available replicas
- Desired replicas

This dataset describes how Kubernetes resources evolve during autoscaling.

Typical applications include:

- workload prediction
- resource forecasting
- autoscaler evaluation
- time-series analysis

---

# Dataset 2 — service_metrics.csv

This dataset is collected from **Jaeger**.

Each record summarizes inbound requests received by one service during one sampling interval.

The recorded metrics include:

- request count
- error count
- P50 latency
- P95 latency

This dataset captures the runtime behavior and service quality experienced by individual microservices.

Typical applications include:

- latency prediction
- service health monitoring
- anomaly detection
- SLO analysis

---

# Dataset 3 — edge_metrics.csv

This dataset is also collected from **Jaeger**.

Instead of monitoring individual services, it records communication between services.

Each record represents one directed edge in the service dependency graph.

Recorded metrics include:

- source service
- destination service
- call count
- error count
- P50 latency
- P95 latency

Example:

```text
frontend
      ↓
productcatalogservice
      ↓
currencyservice
```

This dataset enables reconstruction of the dynamic communication graph of the application.

Typical applications include:

- Graph Neural Networks (GNN)
- graph forecasting
- service dependency analysis
- communication bottleneck detection

---

# Metadata Files

## meta.json

Stores experiment-level information including:

- scenario
- run identifier
- random seed
- experiment duration
- sampling interval
- frontend endpoint
- autoscaler
- initial replica count
- exit status

---

## driver_report.json

Contains workload generation statistics:

- planned arrivals
- started arrivals
- completed arrivals
- dropped arrivals
- maximum concurrent sessions
- expected average requests per second

This file verifies whether the load generator successfully replayed the designed workload.

---

## collect_report.json

Reports the quality of telemetry collection, including:

- collected Prometheus series
- Jaeger coverage
- truncated trace windows
- exported row counts
- collection duration

It is primarily used for validating dataset completeness.

---

# Intended Applications

This dataset is designed for research on:

- Kubernetes autoscaling
- workload forecasting
- Graph Neural Networks
- Reinforcement Learning
- predictive autoscaling
- anomaly detection
- microservice performance modeling
- service dependency analysis
- multivariate time-series forecasting

Because the dataset simultaneously captures resource utilization, request latency, service interactions, and autoscaling behavior under diverse stochastic workloads, it provides a comprehensive benchmark for intelligent cloud resource management and microservice performance prediction.