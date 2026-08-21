/**
 * Bind/release helpers for the cloak tool.
 *
 * Not a registered OMP tool. `omp/cloak.ts` is the only agent-facing name.
 * Sidecar path stays `<session>.bind-profile.json` (contract).
 */
import { existsSync, readFileSync, realpathSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export type BindParams = {
	profile?: string;
	site?: string;
	account?: string;
	scratch?: boolean;
};

export type ExecFn = (
	command: string,
	args: string[],
	options?: { timeout?: number; cwd?: string },
) => Promise<{ stdout: string; stderr: string; code: number }>;

export type SessionKey = object | string;

export type BindState = {
	leaseId: string;
	browserLeaseId?: string;
	targetId?: string;
	targetLeaseId?: string;
	profile?: string;
	root: string;
	cdp: string;
	worker: string;
	socket: string;
	/** targetId → leaseId for every tab this navigator currently holds */
	held?: Record<string, string>;
};

export type SessionCtx = { sessionManager?: { getSessionFile?: () => string | null } };

export type BindResult =
	| { ok: true; reused: boolean; state: BindState; used: string }
	| { ok: false; text: string };

export type ReleaseResult = { ok: boolean; text: string };

export type AliveCheck = (state: BindState) => Promise<boolean>;

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

export function resolveOpsRoot(): string {
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
		"browser-ops root not found. set BROWSER_OPS_ROOT or keep cloak.ts inside the checkout (symlink ok).",
	);
}

export function sessionKey(ctx: SessionCtx): SessionKey {
	const file = ctx.sessionManager?.getSessionFile?.();
	return file || ctx.sessionManager || "orphan";
}

export function sidecarPath(ctx: SessionCtx): string | null {
	const file = ctx.sessionManager?.getSessionFile?.();
	return file ? `${file}.bind-profile.json` : null;
}

export function readSidecar(ctx: SessionCtx): BindState | undefined {
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

export function writeSidecar(ctx: SessionCtx, state: BindState): void {
	const path = sidecarPath(ctx);
	if (!path) return;
	writeFileSync(path, JSON.stringify(state));
}

export function clearSidecar(ctx: SessionCtx): void {
	const path = sidecarPath(ctx);
	if (!path || !existsSync(path)) return;
	try {
		unlinkSync(path);
	} catch {
		/* best-effort */
	}
}

export function daemonSocketPath(root: string, worker: string): string {
	return join(root, "state", worker, "daemon.sock");
}

export function sidecarComplete(state: BindState | undefined): state is BindState {
	if (!state) return false;
	return Boolean(state.leaseId && state.cdp && state.worker && state.socket && state.targetId);
}

export async function cdpHttpAlive(exec: ExecFn, cdp: string | undefined): Promise<boolean> {
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

export async function runJson(
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

export async function cardsMarkdown(exec: ExecFn, root: string): Promise<string> {
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

function asRecord(v: unknown): Record<string, unknown> {
	return v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
}

function asString(v: unknown): string {
	return typeof v === "string" ? v : v == null ? "" : String(v);
}

export function stateFromLaunch(
	data: Record<string, unknown>,
	root: string,
	used: string,
	scratch: boolean,
): BindState | { error: string } {
	const env = asRecord(data.env);
	const lease = asRecord(data.lease);
	const spawn = asRecord(data.spawn);
	const browserLease = asRecord(data.browser_lease);
	const resources = {
		...asRecord(browserLease.resources),
		...asRecord(lease.resources),
	};
	const leaseId = asString(env.BROWSERCTL_LEASE_ID || lease.lease_id);
	const cdp = asString(env.BROWSER_CDP_URL || resources.cdp_url);
	const worker = asString(
		env.BROWSER_HARNESS_WORKER || lease.worker_id || spawn.worker_id || resources.worker_id,
	);
	const socket = asString(resources.socket) || (worker ? daemonSocketPath(root, worker) : "");
	const targetId = asString(env.BROWSERCTL_TARGET_ID || lease.target_id || spawn.target_id);
	if (!leaseId || !cdp || !worker || !socket || !targetId) {
		return {
			error:
				`bind incomplete (${used}): lease=${leaseId || "missing"} worker=${worker || "missing"} ` +
				`socket=${socket || "missing"} target=${targetId || "missing"} cdp=${cdp ? "set" : "missing"}.`,
		};
	}
	return {
		leaseId,
		browserLeaseId: asString(env.BROWSERCTL_BROWSER_LEASE_ID || lease.browser_lease_id || spawn.browser_lease_id) || undefined,
		targetId,
		targetLeaseId: asString(env.BROWSERCTL_TARGET_LEASE_ID || leaseId) || undefined,
		profile: asString(env.BROWSERCTL_PROFILE_NAME) || (scratch ? undefined : used),
		root,
		cdp,
		worker,
		socket,
		held: { [targetId]: leaseId },
	};
}

export function createBinder(exec: ExecFn, opts?: { isAlive?: AliveCheck }) {
	const binds = new Map<SessionKey, BindState>();

	function loadState(ctx: SessionCtx): BindState | undefined {
		const key = sessionKey(ctx);
		return binds.get(key) ?? readSidecar(ctx);
	}

	function storeState(ctx: SessionCtx, state: BindState): void {
		binds.set(sessionKey(ctx), state);
		writeSidecar(ctx, state);
	}

	function dropState(ctx: SessionCtx): void {
		binds.delete(sessionKey(ctx));
		clearSidecar(ctx);
	}

	async function isAlive(state: BindState): Promise<boolean> {
		if (!sidecarComplete(state)) return false;
		if (opts?.isAlive) return opts.isAlive(state);
		return cdpHttpAlive(exec, state.cdp);
	}

	async function releaseState(ctx: SessionCtx): Promise<ReleaseResult> {
		const state = loadState(ctx);
		if (!state) {
			return { ok: true, text: "No bind to release." };
		}
		const ids = new Set<string>([state.leaseId, ...Object.values(state.held || {})].filter(Boolean));
		const failed: string[] = [];
		for (const lid of ids) {
			const released = await runJson(exec, ["release", "--lease", lid], state.root);
			if (!released.ok) failed.push(`${lid}: ${released.raw.slice(0, 200)}`);
		}
		dropState(ctx);
		if (failed.length) {
			return {
				ok: false,
				text: `release failed: ${failed.join("; ")}`,
			};
		}
		return {
			ok: true,
			text: `Released ${ids.size} target lease(s)` +
				(state.targetId ? ` last=${state.targetId}` : "") +
				". shared browser stays up if other target leases remain.",
		};
	}

	async function bind(ctx: SessionCtx, params: BindParams): Promise<BindResult> {
		let root: string;
		try {
			root = resolveOpsRoot();
		} catch (e) {
			return { ok: false, text: e instanceof Error ? e.message : String(e) };
		}

		const existing = loadState(ctx);
		if (existing) {
			if (await isAlive(existing)) {
				storeState(ctx, existing);
				return { ok: true, reused: true, state: existing, used: existing.profile || "(scratch)" };
			}
			dropState(ctx);
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
				exec,
				["profiles", "resolve", params.site.trim(), ...(params.account ? ["--account", params.account] : [])],
				root,
			);
			if (!resolved.ok) {
				const code = (resolved.data.error as { code?: string } | undefined)?.code || "RESOLVE_FAILED";
				if (code === "PROFILE_AMBIGUOUS") {
					return {
						ok: false,
						text: `Ambiguous site=${params.site}: pass account=. ${resolved.raw.slice(0, 800)}`,
					};
				}
				if (code === "PROFILE_NOT_FOUND") {
					return {
						ok: false,
						text:
							`No named profile for site=${params.site}. ` +
							`Call cloak action=bind with scratch=true for a public page, ` +
							`or yield propose_profile / propose_associate if login is required. ` +
							resolved.raw.slice(0, 600),
					};
				}
				return { ok: false, text: `resolve failed: ${resolved.raw.slice(0, 800)}` };
			}
			const name = String(resolved.data.name || "");
			if (!name) {
				return { ok: false, text: `resolve returned no name: ${resolved.raw.slice(0, 400)}` };
			}
			argv.push("--mode", "persistent", "--profile", name);
			used = name;
		} else {
			return {
				ok: false,
				text: "Need action=bind with profile=, site=, or scratch=true (or action=release).\n\nRegistry:\n" +
					(await cardsMarkdown(exec, root)),
			};
		}

		const launched = await runJson(exec, argv, root);
		if (!launched.ok) {
			return { ok: false, text: `bind failed (${used}): ${launched.raw.slice(0, 1200)}` };
		}
		const built = stateFromLaunch(launched.data, root, used, Boolean(params.scratch));
		if ("error" in built) {
			return { ok: false, text: `${built.error} ${launched.raw.slice(0, 600)}` };
		}
		storeState(ctx, built);
		return { ok: true, reused: false, state: built, used };
	}

	return { loadState, storeState, dropState, releaseState, bind };
}
