/**
 * bind_profile — harness-owned browserctl bind for navigator subagents.
 *
 * Source of truth lives in this repo. ~/.omp/agent/tools/bind-profile.ts
 * should be a symlink here. Navigators must not shell out to browserctl.
 *
 * Attach is the upstream browser parameter: app.cdp_url from this tool's result.
 * The Cloak stays up across yields. release=true closes it now.
 * onSession(shutdown) reaps on real teardown (idle-TTL park, kill, process exit).
 */
import { existsSync, readFileSync, realpathSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type BindParams = {
	profile?: string;
	site?: string;
	account?: string;
	scratch?: boolean;
	release?: boolean;
};

type ExecFn = (
	command: string,
	args: string[],
	options?: { timeout?: number; cwd?: string },
) => Promise<{ stdout: string; stderr: string; code: number }>;

type SessionKey = object | string;

type BindState = {
	leaseId: string;
	browserLeaseId?: string;
	targetId?: string;
	targetLeaseId?: string;
	profile?: string;
	root: string;
	cdp: string;
};

type SessionCtx = { sessionManager?: { getSessionFile?: () => string | null } };

function walkForRoot(start: string): string | undefined {
	let dir = start;
	for (let i = 0; i < 8; i++) {
		if (
			existsSync(join(dir, "bin", "browserctl")) &&
			existsSync(join(dir, "browserctl", "cli.py"))
		) {
			return dir;
		}
		const parent = dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	return undefined;
}

function resolveOpsRoot(): string {
	const env = (process.env.BROWSER_OPS_ROOT || "").trim();
	if (env) return env;
	try {
		const toolFile = fileURLToPath(import.meta.url);
		const real = realpathSync(toolFile);
		const fromTool = walkForRoot(dirname(real)) || walkForRoot(dirname(toolFile));
		if (fromTool) return fromTool;
	} catch {
		/* import.meta.url / realpath unavailable */
	}
	const fromCwd = walkForRoot(process.cwd());
	if (fromCwd) return fromCwd;
	throw new Error(
		"browser-ops root not found. set BROWSER_OPS_ROOT or keep bind-profile.ts inside the checkout (symlink ok).",
	);
}

function sessionKey(ctx: SessionCtx): SessionKey {
	const file = ctx.sessionManager?.getSessionFile?.();
	return file || ctx.sessionManager || "orphan";
}

function sidecarPath(ctx: SessionCtx): string | null {
	const file = ctx.sessionManager?.getSessionFile?.();
	return file ? `${file}.bind-profile.json` : null;
}

function readSidecar(ctx: SessionCtx): BindState | undefined {
	const path = sidecarPath(ctx);
	if (!path || !existsSync(path)) return undefined;
	try {
		const data = JSON.parse(readFileSync(path, "utf8")) as BindState;
		if (data?.leaseId && data.root) return data;
	} catch {
		/* ignore corrupt sidecar */
	}
	return undefined;
}

function writeSidecar(ctx: SessionCtx, state: BindState): void {
	const path = sidecarPath(ctx);
	if (!path) return;
	writeFileSync(path, JSON.stringify(state));
}

function clearSidecar(ctx: SessionCtx): void {
	const path = sidecarPath(ctx);
	if (!path || !existsSync(path)) return;
	try {
		unlinkSync(path);
	} catch {
		/* best-effort */
	}
}

async function cdpHttpAlive(exec: ExecFn, cdp: string | undefined): Promise<boolean> {
	const raw = (cdp || "").trim();
	if (!raw) return false;
	let origin: string;
	try {
		const u = new URL(raw);
		if (u.protocol !== "http:" && u.protocol !== "https:") return false;
		origin = u.origin;
	} catch {
		return false;
	}
	const r = await exec("python3", ["-c", (
		"import sys,urllib.request\n"
		+ "u=sys.argv[1].rstrip('/')+'/json/version'\n"
		+ "try:\n"
		+ " urllib.request.urlopen(u, timeout=1)\n"
		+ " sys.exit(0)\n"
		+ "except Exception:\n"
		+ " sys.exit(1)\n"
	), origin], { timeout: 3_000 });
	return r.code === 0;
}

async function runJson(
	exec: ExecFn,
	argv: string[],
	root: string,
): Promise<{ ok: boolean; raw: string; data: Record<string, unknown> }> {
	const bin = existsSync(join(root, "bin", "browserctl")) ? join(root, "bin", "browserctl") : "browserctl";
	const r = await exec(bin, [...argv, "--json", "--root", root], {
		timeout: 60_000,
	});
	const raw = `${r.stdout || ""}${r.stderr || ""}`.trim();
	let data: Record<string, unknown> = {};
	try {
		data = JSON.parse(r.stdout || "{}") as Record<string, unknown>;
	} catch {
		data = { parse_error: true, stdout: r.stdout, stderr: r.stderr };
	}
	return { ok: r.code === 0 && data.ok !== false, raw, data };
}

async function cardsMarkdown(exec: ExecFn, root: string): Promise<string> {
	const r = await runJson(exec, ["profiles", "cards"], root);
	if (r.ok && typeof r.data.markdown === "string") return r.data.markdown;
	if (r.ok && r.raw && !r.raw.startsWith("{")) return r.raw;
	const listed = await runJson(exec, ["profiles", "list"], root);
	const profiles = (listed.data.profiles as Array<Record<string, unknown>>) || [];
	if (!profiles.length) return "(no named profiles in registry)";
	return profiles
		.map(p => {
			const name = String(p.name || "?");
			const desc = String(p.description || "(no description)");
			const assocs = Array.isArray(p.associations) ? p.associations : [];
			const assocLine = assocs
				.map((a: Record<string, string>) => `${a.site}${a.account ? ` / ${a.account}` : ""}`)
				.join(", ");
			const verified = p.last_verified_at ? String(p.last_verified_at) : "never";
			return `## ${name}\n${desc}\nAuthed: ${assocLine || "(none)"}\nLast verified: ${verified}`;
		})
		.join("\n\n");
}

export default function bindProfileTool(pi: { exec: ExecFn }) {
	const binds = new Map<SessionKey, BindState>();

	function loadState(ctx: SessionCtx): BindState | undefined {
		const key = sessionKey(ctx);
		return binds.get(key) ?? readSidecar(ctx);
	}

	async function releaseState(ctx: SessionCtx): Promise<{ ok: boolean; text: string }> {
		const key = sessionKey(ctx);
		const state = loadState(ctx);
		if (!state) {
			return { ok: true, text: "No bind to release." };
		}
		const released = await runJson(pi.exec, ["release", "--lease", state.leaseId], state.root);
		binds.delete(key);
		clearSidecar(ctx);
		if (!released.ok) {
			return {
				ok: false,
				text: `release failed lease=${state.leaseId}: ${released.raw.slice(0, 800)}`,
			};
		}
		return {
			ok: true,
			text: `Released target lease=${state.leaseId}` +
				(state.targetId ? ` target=${state.targetId}` : "") +
				". shared browser stays up if other target leases remain.",
		};
	}

	return {
		name: "bind_profile",
		label: "Bind browser profile",
		loadMode: "essential" as const,
		description:
			"Bind this navigator session to a browser-ops Cloak profile (or ephemeral scratch). " +
			"Resolve by site, or name a registered profile, or scratch=true for anonymous. " +
			"Then browser open with app.cdp_url set to the returned cdp_url. " +
			"The browser stays up after yield for follow-ups. release=true only to close it now. " +
			"Do not run browserctl yourself.",
		parameters: {
			type: "object",
			properties: {
				profile: { type: "string", description: "Registered profile name (optional)" },
				site: { type: "string", description: "Site key to resolve, e.g. github.com" },
				account: { type: "string", description: "Exact account label when site has account-scoped faces" },
				scratch: { type: "boolean", description: "Bind an ephemeral anonymous scratch (wipe on cleanup)" },
				release: { type: "boolean", description: "Close this job's Cloak now. Omit to keep it for follow-ups." },
			},
		},
		async execute(_id: string, params: BindParams, _onUpdate: unknown, ctx: SessionCtx) {
			if (params.release) {
				const out = await releaseState(ctx);
				return { content: [{ type: "text" as const, text: out.text }] };
			}

			let root: string;
			try {
				root = resolveOpsRoot();
			} catch (e) {
				return {
					content: [{ type: "text" as const, text: e instanceof Error ? e.message : String(e) }],
				};
			}
			const key = sessionKey(ctx);
			const existing = loadState(ctx);
			if (existing) {
				if (!(await cdpHttpAlive(pi.exec, existing.cdp))) {
					binds.delete(key);
					clearSidecar(ctx);
				} else {
					binds.set(key, existing);
					return {
						content: [
							{
								type: "text" as const,
								text:
									`Already bound lease=${existing.leaseId} profile=${existing.profile || "(scratch)"} ` +
									`cdp_url=${existing.cdp}` +
									(existing.targetId ? ` target=${existing.targetId}` : "") +
									`. Open with app.cdp_url=${existing.cdp}` +
									(existing.targetId ? ` app.target_id=${existing.targetId}` : "") +
									`. Attach only to the leased target. release=true drops this target, not sibling navigators.`,
							},
						],
					};
				}
			}
			const argv: string[] = ["launch", "--owner", "omp-nav"];
			let used = "scratch";

			if (params.scratch) {
				argv.push("--mode", "one_shot", "--kind", "scratch", "--label", "nav-ephemeral");
				used = "scratch (ephemeral)";
			} else if (params.profile?.trim()) {
				argv.push("--mode", "persistent", "--profile", params.profile.trim());
				used = params.profile.trim();
			} else if (params.site?.trim()) {
				const resolved = await runJson(
					pi.exec,
					["profiles", "resolve", params.site.trim(), ...(params.account ? ["--account", params.account] : [])],
					root,
				);
				if (!resolved.ok) {
					const code = (resolved.data.error as { code?: string } | undefined)?.code || "RESOLVE_FAILED";
					if (code === "PROFILE_AMBIGUOUS") {
						return {
							content: [
								{
									type: "text" as const,
									text: `Ambiguous site=${params.site}: pass account=. ${resolved.raw.slice(0, 800)}`,
								},
							],
						};
					}
					if (code === "PROFILE_NOT_FOUND") {
						return {
							content: [
								{
									type: "text" as const,
									text:
										`No named profile for site=${params.site}. ` +
										`Call bind_profile with scratch=true for a public page, ` +
										`or yield propose_profile / propose_associate if login is required. ` +
										resolved.raw.slice(0, 600),
								},
							],
						};
					}
					return { content: [{ type: "text" as const, text: `resolve failed: ${resolved.raw.slice(0, 800)}` }] };
				}
				const name = String(resolved.data.name || "");
				if (!name) {
					return { content: [{ type: "text" as const, text: `resolve returned no name: ${resolved.raw.slice(0, 400)}` }] };
				}
				argv.push("--mode", "persistent", "--profile", name);
				used = name;
			} else {
				return {
					content: [
						{
							type: "text" as const,
							text: "Need profile=, site=, scratch=true, or release=true.\n\nRegistry:\n" + (await cardsMarkdown(pi.exec, root)),
						},
					],
				};
			}

			const launched = await runJson(pi.exec, argv, root);
			if (!launched.ok) {
				return {
					content: [{ type: "text" as const, text: `bind failed (${used}): ${launched.raw.slice(0, 1200)}` }],
				};
			}
			const env = (launched.data.env as Record<string, string>) || {};
			const lease = (launched.data.lease as Record<string, unknown>) || {};
			const leaseId = String(env.BROWSERCTL_LEASE_ID || lease.lease_id || "");
			const cdp = String(env.BROWSER_CDP_URL || "");
			if (!leaseId || !cdp) {
				return {
					content: [
						{
							type: "text" as const,
							text: `bind incomplete (${used}): lease=${leaseId || "missing"} cdp_url=${cdp || "missing"}. ${launched.raw.slice(0, 600)}`,
						},
					],
				};
			}

			const state: BindState = {
				leaseId,
				browserLeaseId: String(env.BROWSERCTL_BROWSER_LEASE_ID || lease.browser_lease_id || ""),
				targetId: String(env.BROWSERCTL_TARGET_ID || lease.target_id || ""),
				targetLeaseId: String(env.BROWSERCTL_TARGET_LEASE_ID || leaseId),
				profile: env.BROWSERCTL_PROFILE_NAME || (params.scratch ? undefined : used),
				root,
				cdp,
			};
			binds.set(key, state);
			writeSidecar(ctx, state);

			return {
				content: [
					{
						type: "text" as const,
						text:
							`Bound ${used}. lease=${leaseId}` +
							(state.targetId ? ` target=${state.targetId}` : "") +
							(state.browserLeaseId ? ` browser_lease=${state.browserLeaseId}` : "") +
							` cdp_url=${cdp}. ` +
							`Next: browser open with app.cdp_url=${cdp}` +
							(state.targetId ? ` app.target_id=${state.targetId}` : "") +
							` (no app.path, no app.relay). Attach only to the leased target; never the first/visible tab. ` +
							`Same profile may host other navigators on other targets. release=true drops this target only.`,
					},
				],
			};
		},
		async onSession(event: { reason: string }, ctx: SessionCtx) {
			if (event.reason !== "shutdown") return;
			await releaseState(ctx);
		},
	};
}
