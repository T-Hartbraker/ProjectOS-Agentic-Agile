# ProjectOS Queue Policy
Queues:
PM
ARCHITECTURE
DELIVERY
ASSURANCE_FUNCTIONAL
ASSURANCE_INTEGRATION
ASSURANCE_SECURITY
ASSURANCE_QUALITY
RELEASE

Rules:
- Every job is bound to exactly one project_human_id and repository_root.
- PM coordinates; it does not silently consume DELIVERY work.
- DELIVERY cannot self-approve or self-release.
- Assurance references the exact candidate revision.
- Independent READY jobs may run concurrently, including jobs from different repositories.
- Jobs from different projects may never share a worktree or projectctl database.
- Failed assurance creates project-local governed defect/rework state.
