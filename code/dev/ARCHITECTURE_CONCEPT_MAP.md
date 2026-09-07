# CService Concept Map

## Revision Control

- Revision ID: `r2`
- Scope: repository-wide conceptual model for the current workspace state
- Purpose: provide a stable, top-down structure that can be revised repeatedly without changing the document's shape
- Reading rule: each layer reveals only the next level of detail; missing pieces are listed explicitly as gaps instead of being inferred

## 1. Executive View

This repository is a backend-centered customer-service platform built around FastAPI, async SQLAlchemy, and a set of domain routers for users, bookings, chat, and system metadata. The backend also exposes deeper analytics, retention, lifecycle, and ecosystem metadata that define the product's operational intelligence layer.

The implementation is best understood as three overlapping backend systems:

1. Identity, account, and auth control plane
2. Booking, chat, and interaction state plane
3. Analytics, retention, and operational insight plane

The current workspace contains both implemented backend surfaces and requirement artifacts that describe a broader intended product. The document therefore distinguishes between:

- Implemented behavior
- Intended behavior from requirement files
- Gaps, mismatches, or duplicated concepts

## 2. Top-Level System Shape

### 2.1 Frontend

The frontend exists in the repository, but it is outside the scope of this revision. This concept map focuses on backend implementation and backend-adjacent requirement artifacts only.

### 2.2 Backend

The backend is a FastAPI application with async SQLAlchemy, token-based auth, domain routers, service helpers, and metadata endpoints. It is organized around user identity, booking lifecycle management, chat analytics, retention intelligence, and ecosystem readiness reporting.

Primary entry chain:

- `fastapi/app/main.py`
- `fastapi/app/db.py`
- `fastapi/app/models.py`
- `fastapi/app/routers/users.py`
- `fastapi/app/routers/bookings.py`
- `fastapi/app/routers/chat.py`

Primary service layer:

- `fastapi/app/services/bookings.py`
- `fastapi/app/services/chat_analytics.py`
- `fastapi/app/services/retention.py`

Primary schema layer:

- `fastapi/app/schemas/schemas.py`
- `fastapi/app/schemas/chat.py`
- `fastapi/app/deps.py`

### 2.3 Requirement Layer

The `requirement/` folder acts like a product brief and intended architecture archive. It describes menus, screens, multilingual behavior, testing expectations, and a service-planning flow that does not fully match the current app routing.

Key artifacts:

- `requirement/core_structure.json`
- `requirement/services_and_planning.json`
- `requirement/multilingual_and_testing.json`
- `requirement/security_spec.js`

## 4. Backend Concept Model

### 4.1 Startup and Environment

`fastapi/app/main.py` constructs the app, configures CORS, verifies the database on startup, and registers exception handling. It exposes metadata endpoints that describe the app, capabilities, ecosystem status, and authenticated probe routes.

At a conceptual level, the backend startup owns:

- App identity and version
- Database readiness
- Cross-origin access policy
- Global error shaping
- Router registration

### 4.2 Data and Domain Substrate

`fastapi/app/models.py` defines the persisted data model that everything else builds on. The important clusters are:

- Identity and access: `User`
- Conversation history: `ChatHistory`
- Booking lifecycle: `Booking`, `BookingEvent`, `BookingStatus`, `ServiceType`
- Behavioral signals: `InteractionSignal`
- Retention history: `RetentionSnapshot`

These objects show that the backend is not only transactional. It also preserves state transitions and interaction traces so the system can explain customer behavior over time.

### 4.3 Identity and Auth API

`fastapi/app/routers/users.py` supports registration, login, current-user lookup, and password change. This is the account control plane for the app.

`fastapi/app/deps.py` resolves the authenticated user from the bearer token and enforces the admin boundary when needed.

Important behaviors:

- JWT decoding via the shared security secret
- Database-backed user lookup by username
- Admin-only enforcement through `get_current_admin_user`

### 4.4 Booking Lifecycle API

`fastapi/app/routers/bookings.py` supports booking creation, listing, reading, editing, deleting, and audit/history summaries. It also exposes admin-style summary analytics. The router is backed by `fastapi/app/services/bookings.py`, which contains reusable lifecycle rules.

Important service behavior:

- Booking updates are normalized before persistence
- Status transitions are validated before mutation
- Transition helpers record booking events alongside state changes
- Ownership checks are centralized in the service layer

This router is the clearest implementation of a lifecycle model rather than a simple CRUD surface.

### 4.5 Chat Analytics and Recovery API

`fastapi/app/routers/chat.py` is the broadest surface. It includes chat history, sentiment analysis, interaction insights, dissatisfaction recovery, loyalty recovery, retention dashboards, maintenance reports, risk profiles, system priorities, monetization cohorts, operational readiness views, and the consolidated retention snapshot operations report. The heavy lifting is concentrated in `fastapi/app/services/chat_analytics.py` and `fastapi/app/services/retention.py`.

Important service behavior:

- Sentiment scoring drives recovery classification
- Keyword, recency, booking state, and history analysis produce policy-area insights
- Recovery reports turn dissatisfaction signals into explicit action plans and retention recommendations
- Recovery outputs now carry evidence summaries, source labels, and stronger policy-area recommendations
- Retention and cohort reports distinguish loyalty, monetization readiness, and signal pressure as separate dimensions
- Retention snapshots summarize loyalty, churn risk, and lifecycle stage
- Retention reports can derive deltas and trends from persisted snapshots
- Retention snapshot operations reports make freshness, cleanup, and readiness explicit

The chat router is therefore both a conversation API and a business-intelligence API.

### 4.6 Metadata and Ecosystem Layer

`fastapi/app/main.py` exposes capability and ecosystem endpoints that describe the backend as a system of cooperating subservices.

This layer is important because it turns the backend into an observable platform rather than a hidden API.

Key surfaces:

- `/meta` for app identity
- `/meta/capabilities` for capability summaries
- `/meta/ecosystem` for subservice status
- `/meta/probe/routes` for authenticated route probing
- `/chat/admin/snapshot-operations-report` for consolidated snapshot operations reporting

### 4.7 Schema Layer

`fastapi/app/schemas/schemas.py` and `fastapi/app/schemas/chat.py` define the shape of all request and response payloads.

This layer is the contract boundary between routers, services, and clients. It formalizes:

- Booking lifecycle payloads
- User and token payloads
- Analytics, recovery, and retention reports
- Ecosystem and capability reports

## 5. Requirement-Layer Intent

The requirement files describe a more formal customer-service product language than the backend currently exposes directly.

### 5.1 Intended Service Scope

`requirement/core_structure.json` and `requirement/services_and_planning.json` describe a broader customer-service system centered on booking, chat assistance, planning, and language-aware behavior. These artifacts are useful as product intent, but they are not the primary implementation surface in this revision.

### 5.2 Intended Multilingual Model

The multilingual spec expects language files and selector-driven switching among at least English, Spanish, and French. Backend support for language-aware behavior exists only indirectly through stored user preferences and request handling patterns; a centralized localization service is not yet evident in the backend.

## 6. Layered Walkthrough From User Intent to Data

### Layer 1: Incoming request

A client or internal tool sends requests for auth, bookings, chat, or metadata.

### Layer 2: Dependency resolution

FastAPI dependencies resolve the current user and database session.

### Layer 3: Domain mutation or read

Routers validate the request, apply business rules, and load or mutate the relevant model.

### Layer 4: Persistence

SQLAlchemy persists the change or reads back the requested state.

### Layer 5: Derived analytics

Services and metadata endpoints compute summaries, retention views, cohort reports, or capability descriptions.

### Layer 6: Operational insight

Metadata routes surface ecosystem readiness, health summaries, and authenticated route probes for internal coordination.

## 7. Stable Gap Register

These are the main gaps that should remain visible across revisions.

### 7.1 Requirement-to-backend mismatch

The requirement files describe a broader product surface than the backend currently exposes through implemented routes.

### 7.2 Chat contract stabilization

The chat API now exposes explicit recovery and retention schema contracts for dissatisfaction, loyalty, retention dashboard, and retention operations flows. Clients should still validate against the router, but the contract is no longer only implied by downstream service behavior.

### 7.3 Translation and locale gaps

The requirements imply a stronger localization layer than the backend currently implements. User preference storage exists, but centralized localization services are not yet explicit.

### 7.4 Backend breadth exceeds implemented UI

The backend exposes rich retention and ecosystem endpoints, and the recovery/retention dashboard and retention operations surfaces are now explicit in the schema layer. Some deeper operational views are still primarily visible through metadata rather than through dedicated product flows.

## 8. Evidence Index

This section is the stable entry point for future revision passes.

### Backend anchors

- [fastapi/app/main.py](fastapi/app/main.py)
- [fastapi/app/models.py](fastapi/app/models.py)
- [fastapi/app/routers/users.py](fastapi/app/routers/users.py)
- [fastapi/app/routers/bookings.py](fastapi/app/routers/bookings.py)
- [fastapi/app/routers/chat.py](fastapi/app/routers/chat.py)

### Requirement anchors

- [requirement/core_structure.json](requirement/core_structure.json)
- [requirement/services_and_planning.json](requirement/services_and_planning.json)
- [requirement/multilingual_and_testing.json](requirement/multilingual_and_testing.json)

## 9. Revision Rules

Use the same document shape for future updates:

1. Update the executive summary only if the architecture changes materially.
2. Preserve the layered order from shell to gaps.
3. Add new facts under the relevant layer instead of rewriting the entire document.
4. Keep gap items explicit until they are verified resolved.
5. Add a new revision ID at the top when the document is substantially reworked.

## 10. Current Assessment

The repository is functionally a backend-first customer-service platform with an underlayer of analytics and retention intelligence. The product story is coherent, but the implementation is not perfectly unified yet. The strongest conceptual boundary is the backend domain model, while the weakest boundary is the mismatch between requirements and the currently implemented backend contract.

## 11. Expansion Priorities

The following expansion areas are forward-looking only. Existing implemented behavior belongs in the earlier architecture sections; this section should describe what still needs to be built or hardened.

### 11.1 Priority 1: Recovery and Dissatisfaction Loop

Implemented:

- Sentiment and policy-area scoring now weight recency, booking state, repeated friction, and support language more explicitly
- Recovery outputs now include evidence summaries and source labels alongside policy recommendations and next steps
- Interaction summaries keep loyalty, monetization readiness, and churn risk separate so recovery can be interpreted without collapsing all signals into one score
- Interaction metadata now carries repeated-message counts, booking-state counts, and aggregate signal strength for later explanation
- Interaction signals can be stored with source, area, score, and recommendation so recovery decisions remain traceable

Open gaps:

- Add explicit measurement endpoints or persisted aggregates for complaint recurrence, time-to-recovery, unresolved issue rate, and recovery acceptance rate
- Store richer per-signal evidence summaries in the database instead of only in derived response payloads

### 11.2 Priority 2: Loyalty Cohorts and Repeat-Use Intelligence

Implemented:

- Cohorts are now driven by both interaction history and stored signals
- Retention cohort and monetization cohort outputs now include explicit cohort rules
- Cohort drilldowns can expose loyalty score, signal score, monetization readiness, recent booking state, and strongest risk area
- Monetization readiness remains separate from loyalty and churn risk in the summary model

Open gaps:

- Measure repeat session rate, cohort migration rate, task re-entry after completion, positive snapshot delta rate, and signal-score trend by cohort

### 11.3 Priority 3: Booking Lifecycle Safety

Implemented:

- Booking updates are normalized before persistence
- Status transitions are validated before mutation
- Transition helpers record booking events alongside state changes
- Ownership checks are centralized in the service layer

Open gaps:

- Tighten create, edit, and delete paths so they all validate the same core booking shape before persistence and produce the same history trail
- Add audit summaries that can explain who changed what, when the change happened, and which state moved to which state
- Track validation failure rate, retry frequency, transition rejection rate, event-count accuracy, and audit completeness

### 11.4 Priority 4: Monitoring, Capability, and Platform Insight Products

Implemented:

- Interaction summaries now explain the most important issues, the strongest positives, and the recommended focus area for admins and support
- System improvement packs now carry owner hints, impact level, rationale, and concrete recommendations
- Weighted monitoring reports are tied to a small set of high-value dimensions such as reliability, response speed, customer activity, and retention
- Capability payloads and ecosystem metadata are aligned with the actual routes and outputs currently exposed by the backend
- The platform surface now advertises the consolidated retention operations report alongside the existing admin retention views

Open gaps:

- Track insight adoption rate, monitoring completeness, recommendation coverage, and trend-report freshness

### 11.5 Priority 5: Retention Snapshot Operations

Implemented:

- Retention snapshot reports now include the latest snapshot and the prior snapshot with explicit loyalty, churn, and lifecycle deltas
- Trend views group snapshot types consistently so fresh versus stale patterns can be compared without ambiguity
- Operations-oriented outputs translate freshness, staleness, and readiness into clear statuses such as watch, hold, escalate, or go/no-go
- Pruning and maintenance outputs are explicit so admins can see how many snapshots were kept, removed, or left untouched during cleanup
- The consolidated retention snapshot operations report is available through a dedicated admin route and reflected in platform metadata

Open gaps:

- Persist the measurement inputs that drive operations readiness so the report can be audited from source data instead of only derived status labels

## 12. Expansion Guardrails

These guardrails describe what future work must preserve. Existing implementations already belong in the earlier sections.

### 12.1 Keep recovery outputs explicit

Do not add recovery, loyalty, or churn outputs that hide the evidence behind an opaque score. Each output should show the main trigger, the strongest evidence, and the action the system expects the user or operator to take next.

### 12.2 Keep each layer measurable

Make every recovery, cohort, monitoring, capability, and snapshot flow traceable. If a report cannot point to concrete inputs such as signals, bookings, snapshots, or route metadata, treat it as provisional. Prefer payloads that include counts, score deltas, and timestamps over narrative-only summaries.

### 12.3 Preserve router and service boundaries

Keep booking routers, chat routers, retention services, and schema contracts separable so future reporting work does not blur ownership. New analysis should usually land in a service helper first, then be exposed through a router, then be formalized in schema.

### 12.4 Protect booking and snapshot state

Validate booking status changes, booking-event creation, and retention-snapshot writes explicitly so the system does not drift into inconsistent states. State-changing paths should reject invalid transitions before commit and should leave an auditable trail when they succeed.

### 12.5 Keep insight surfaces honest

Only expose metadata, summaries, or reports when the underlying data is actually present and fresh enough to support them. If the time window is too small, the signal count too low, or the snapshot set too stale, the response should say that directly.

### 12.6 Keep platform metadata consistent

Keep capability summaries, ecosystem status, and feature lists aligned with the router and service layer. If metadata advertises a route or capability, that route should exist and its output shape should match the advertised purpose.

### 12.7 Keep revision structure stable

Extend these priorities with new to-dos rather than rewriting the whole document.
