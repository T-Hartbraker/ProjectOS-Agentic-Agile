# ProjectOS Software Factory FAT

End-to-end acceptance test for the universal delivery production line.

## Prerequisites

- ProjectOS running with Slack + OpenAI + GitHub configured
- Empty delivery repository registered as a new project
- `project/delivery.json` present (or inferred during project creation)

## FAT flow

### 1. Sponsor intent (Slack)

```
@ChatGPT I want to build a small Windows desktop utility called Example Product.
It should run on Windows x64 and be distributed externally.
```

Approve project creation when proposed.

### 2. Planning and implementation

Let ProjectOS planning/workers complete at least one iteration through DELIVERY and QA.

### 3. Release readiness

```
@ChatGPT Is PRJ-### ready to release?
```

Expect read-only status with gate blockers if any.

### 4. Prepare release (proposal + approval)

```
@ChatGPT prepare PRJ-### version 1.0.0 for release.
```

Approve the persisted proposal.

CLI equivalent:

```text
projectos release prepare --project PRJ-### --release REL-001 --version 1.0.0
```

### 5. Package

```text
projectos release package --record DLV-...
projectos release verify --record DLV-...
```

### 6. Publish (proposal + approval)

```
@ChatGPT release PRJ-###.
```

Approve. ProjectOS publishes to GitHub Releases and posts Slack card.

### 7. Install verification

Download installer from GitHub Release URL in Slack card. Install on clean Windows VM. Launch application.

## First command to start a brand-new test project

In Slack:

```
@ChatGPT I want to start a new software project called Example Product for Windows x64 distribution. Please propose creating it in ProjectOS.
```
