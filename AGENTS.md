# MediaVPN

Independent app; do not modify production MediaHub, FjordVPN or existing tunnels.
Use synthetic WireGuard configurations in tests. Never start a real tunnel without a user-uploaded profile and an explicitly enabled runtime.
Standard Compose is an unprivileged preview without Docker socket or TUN. Runtime override grants Docker-host control and must be documented as privileged.
Keep private keys out of API responses, validation errors, logs and Git. Fresh container health is not proof of public egress IP, forwarding, or leak prevention for attached apps.
Keep one application worker; local lifecycle locks are process-scoped. Test lifecycle, authentication, CSRF and mobile views before publishing.