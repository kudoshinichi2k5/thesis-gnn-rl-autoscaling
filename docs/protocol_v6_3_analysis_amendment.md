# Protocol v6.3 Analysis Amendment: Frontend Downstream Observation Contract

## 1. Structural Rationale
In microservice architectures like Online Boutique, the `frontend` service acts as an API gateway fan-out node. Downstream services exhibit highly asymmetric execution frequencies and latency contributions. Categorizing all downstream callees under an unweighted `downstream-normal` assertion creates false-negative contract failures due to rare callee sampling sparsity.

## 2. Latency Contribution Threshold
Using historical HPA telemetry (`dataset/raw`), callees are classified by their cumulative 95th percentile latency contribution to `frontend`:
- **Important Callees ($\ge 5\%$ Contribution):** `cartservice`, `productcatalogservice`, `recommendationservice`. Must satisfy standard `downstream-normal` criteria.
- **Rare Callees ($< 5\%$ Contribution):** `checkoutservice`, `shippingservice`, `paymentservice`, `emailservice`, `adservice`.

## 3. Amendment Rules for Rare Callees
For rare callees:
1. Expected spans per 30-second window are estimated as:
   $$\text{Expected Spans} = \text{Actual Load} \times \text{Fan-out} \times 0.10$$
2. If actual collected spans $< 3$, the window is flagged as `insufficient_evidence`.
3. An `insufficient_evidence` flag does **NOT** cause a `downstream-normal` failure, nor is it marked as PASS. It is treated as a censored observation window.
4. Latency ceiling violations ($> 500\text{ ms}$) on important callees retain strict fail-closed enforcement.

## 4. Independence from Capacity Estimation ($\mu$)
This amendment is derived strictly from telemetry sampling probability and architectural topology. It is completely independent of service capacity ($\mu$) identification outcomes.