# Agent instructions

At the start of every fresh task in this repository, before inspecting files or
taking task actions, read:

1. `docs/ARCHITECTURE.md`
2. `docs/DEPLOYMENT.md`

Treat those documents as the current operational context for the JavaanFitness
application, AWS development stack, deployment boundaries, Colima-backed SAM
build process, Mini App cache-busting, secrets handling, and data-retention
constraints.

Follow the repository and user instructions if they are more specific or
recent. Do not deploy, modify infrastructure, expose secrets, or change
application behavior unless the task explicitly requires it. Keep generated
`.aws-sam/` artifacts out of commits and preserve retained workout, nutrition,
and history data.
