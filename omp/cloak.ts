/**
 * cloak — bind a leased Cloak browser and drive the leased tab.
 *
 * Hidden unless an agent tools: list (or --tools) names it. Bind via
 * browserctl; drive via json-line on state/<worker>/daemon.sock. Never
 * attach a second CDP client to the shared port.
 *
 * Symlink: ~/.omp/agent/tools/cloak.ts → this file.
 */
import { existsSync } from "node:fs";
import * as net from "node:net";

import {
	cdpHttpAlive,
	createBinder,
	pruneHeldLeases,
	sidecarComplete,
	type BindState,
	type ExecFn,
	type SessionCtx,
} from "./bind-profile.ts";

const DRIVE_ACTIONS = [
	"ping",
	"page_info",
	"navigate",
	"screenshot",
	"click",
	"type",
	"press",
	"scroll",
	"extract",
	"fill",
	"tabs",
	"new_tab",
	"switch_tab",
	"close_tab",
	"dialog",
	"wait_for_load",
	"wait_for_element",
] as const;

type DriveAction = (typeof DRIVE_ACTIONS)[number];

const DRIVE_SET: Record<string, true> = Object.fromEntries(
	DRIVE_ACTIONS.map((a) => [a, true]),
) as Record<string, true>;

const WIRE_ACTION: Record<string, string> = {
	fill: "fill_input",
};

export type CloakParams = {
	action: "bind" | "release" | DriveAction;
	profile?: string;
	site?: string;
	account?: string;
	scratch?: boolean;
	url?: string;
	path?: string;
	x?: number;
	y?: number;
	button?: string;
	clicks?: number;
	text?: string;
	key?: string;
	dy?: number;
	selector?: string;
	attribute?: string;
	timeout?: number;
	accept?: boolean;
	prompt_text?: string;
	/** tab to peek / close (not the bound drive pin) */
	target_id?: string;
};

export type DaemonRequest = {
	action: string;
	target_id?: string;
	[key: string]: unknown;
};

export type DaemonResponse = {
	ok?: boolean;
	code?: string;
	message?: string;
	retryable?: boolean;
	error?: string;
	[key: string]: unknown;
};

export function sendToDaemon(
	socketPath: string,
	request: DaemonRequest,
	timeoutMs = 30_000,
): Promise<DaemonResponse> {
	const { promise, resolve, reject } = Promise.withResolvers<DaemonResponse>();

	const client = net.createConnection(socketPath, () => {
		client.write(JSON.stringify(request) + "\n");
	});

	const timer = setTimeout(() => {
		client.destroy();
		reject(new Error(`Daemon request timed out after ${timeoutMs}ms: ${request.action}`));
	}, timeoutMs);

	let buffer = "";
	client.on("data", (chunk: Buffer) => {
		buffer += chunk.toString();
		const newlineIdx = buffer.indexOf("\n");
		if (newlineIdx >= 0) {
			clearTimeout(timer);
			const line = buffer.substring(0, newlineIdx);
			client.end();
			try {
				resolve(JSON.parse(line) as DaemonResponse);
			} catch {
				reject(new Error(`Failed to parse daemon response: ${line}`));
			}
		}
	});

	client.on("error", (err: Error) => {
		clearTimeout(timer);
		reject(new Error(`Daemon connection failed (${socketPath}): ${err.message}`));
	});

	return promise;
}

function heldMap(sidecar: BindState): Record<string, string> {
	if (sidecar.held && Object.keys(sidecar.held).length) return sidecar.held;
	if (sidecar.targetId && sidecar.leaseId) return { [sidecar.targetId]: sidecar.leaseId };
	return {};
}

function leaseForTarget(sidecar: BindState, targetId: string | undefined): string | undefined {
	const held = heldMap(sidecar);
	if (targetId && held[targetId]) return held[targetId];
	return sidecar.leaseId;
}

export function remintTabsOwnership(result: DaemonResponse, sidecar: BindState): DaemonResponse {
	const tabs = result.tabs;
	if (!Array.isArray(tabs)) return result;
	const held = heldMap(sidecar);
	return {
		...result,
		tabs: tabs.map((tab) => {
			if (!tab || typeof tab !== "object") return tab;
			const t = tab as Record<string, unknown>;
			const tid = String(t.targetId || t.target_id || "");
			if (tid && held[tid]) return { ...t, ownership: "owned_by_me" };
			return t;
		}),
	};
}

export function buildDriveRequest(action: string, sidecar: BindState, params: CloakParams): DaemonRequest {
	const wire = WIRE_ACTION[action] ?? action;
	const dest = action === "switch_tab" || action === "close_tab" ? params.target_id : undefined;
	const req: DaemonRequest = {
		action: wire,
		target_id: dest && heldMap(sidecar)[dest] ? dest : sidecar.targetId,
		lease_id: leaseForTarget(sidecar, dest),
	};

	if (action === "dialog" && params.accept !== undefined) {
		req.action = "dialog_dismiss";
		req.accept = params.accept;
		if (params.prompt_text !== undefined) req.prompt_text = params.prompt_text;
		return req;
	}

	switch (action) {
		case "navigate":
		case "new_tab":
			if (params.url !== undefined) req.url = params.url;
			break;
		case "screenshot":
			if (params.path !== undefined) req.path = params.path;
			break;
		case "click":
			if (params.x !== undefined) req.x = params.x;
			if (params.y !== undefined) req.y = params.y;
			if (params.button !== undefined) req.button = params.button;
			if (params.clicks !== undefined) req.clicks = params.clicks;
			break;
		case "type":
		case "fill":
			if (params.text !== undefined) req.text = params.text;
			if (action === "fill" && params.selector !== undefined) req.selector = params.selector;
			break;
		case "press":
			if (params.key !== undefined) req.key = params.key;
			break;
		case "scroll":
			if (params.x !== undefined) req.x = params.x;
			if (params.y !== undefined) req.y = params.y;
			if (params.dy !== undefined) req.dy = params.dy;
			break;
		case "extract":
		case "wait_for_element":
			if (params.selector !== undefined) req.selector = params.selector;
			if (action === "extract" && params.attribute !== undefined) req.attribute = params.attribute;
			if (action === "wait_for_element" && params.timeout !== undefined) req.timeout = params.timeout;
			break;
		case "wait_for_load":
			if (params.timeout !== undefined) req.timeout = params.timeout;
			break;
		case "dialog":
			if (params.accept !== undefined) req.accept = params.accept;
			if (params.prompt_text !== undefined) req.prompt_text = params.prompt_text;
			break;
		case "switch_tab":
		case "close_tab":
			if (params.target_id !== undefined) req.dest_target_id = params.target_id;
			if (params.path !== undefined) req.path = params.path;
			break;
		default:
			break;
	}
	return req;
}

export async function socketAlive(socket: string | undefined): Promise<boolean> {
	const path = (socket || "").trim();
	if (!path || !existsSync(path)) return false;
	try {
		const r = await sendToDaemon(path, { action: "ping" }, 2_000);
		return !isDaemonError(r);
	} catch {
		return false;
	}
}

export function isDaemonError(result: DaemonResponse): boolean {
	if (result.ok === true) return false;
	if (result.error) return true;
	if (result.code && result.message) return true;
	return false;
}

const OPAQUE_KEYS = new Set([
	"lease_id",
	"leaseId",
	"lease",
	"held_lease_ids",
	"held",
	"browser_lease_id",
	"browserLeaseId",
	"targetLeaseId",
	"released_lease",
	"stolen_from",
]);

export function redactForModel(value: unknown): unknown {
	if (Array.isArray(value)) return value.map(redactForModel);
	if (!value || typeof value !== "object") return value;
	const out: Record<string, unknown> = {};
	for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
		if (OPAQUE_KEYS.has(k)) continue;
		out[k] = redactForModel(v);
	}
	return out;
}

function textResult(text: string, extra?: { isError?: boolean; details?: unknown }) {
	return {
		content: [{ type: "text" as const, text }],
		...(extra?.isError ? { isError: true as const } : {}),
		...(extra?.details !== undefined ? { details: extra.details } : {}),
	};
}

export function applyTabResult(state: BindState, action: string, result: DaemonResponse): BindState {
	const held: Record<string, string> = { ...(state.held || {}) };
	if (state.targetId && state.leaseId && !held[state.targetId]) {
		held[state.targetId] = state.leaseId;
	}
	const tid = typeof result.target_id === "string" ? result.target_id : undefined;
	const lid = typeof result.lease_id === "string" ? result.lease_id : undefined;
	const mode = typeof result.mode === "string" ? result.mode : undefined;

	if (action === "new_tab" && tid && lid) {
		held[tid] = lid;
		return { ...state, targetId: tid, leaseId: lid, targetLeaseId: lid, held };
	}
	if (action === "switch_tab") {
		if (mode === "peek") return state;
		if (tid && lid) {
			held[tid] = lid;
			return { ...state, targetId: tid, leaseId: lid, targetLeaseId: lid, held };
		}
		return state;
	}
	if (action === "close_tab") {
		const closed = typeof result.closed === "string" ? result.closed : tid;
		const released = typeof result.released_lease === "string" ? result.released_lease : undefined;
		if (closed) delete held[closed];
		if (released) {
			for (const [k, v] of Object.entries(held)) {
				if (v === released) delete held[k];
			}
		}
		if (closed && closed === state.targetId) {
			const nextTid = Object.keys(held)[0];
			if (nextTid) {
				return { ...state, targetId: nextTid, leaseId: held[nextTid], targetLeaseId: held[nextTid], held };
			}
			return { ...state, targetId: undefined, held };
		}
		return { ...state, held };
	}
	return state;
}

function bindSummary(state: BindState, used: string, reused: boolean): string {
	const verb = reused ? "Already bound" : "Bound";
	return (
		`${verb} ${used}. target=${state.targetId} worker=${state.worker}. ` +
		`Drive with cloak actions (navigate, click, type, …) — the tool pins the leased tab. ` +
		`tabs lists every page tagged owned_by_me / owned_by / unowned. ` +
		`new_tab opens a tab you own. switch_tab to a sibling is a peek. ` +
		`Do not attach a CDP url. release when the job is done.`
	);
}

export default function cloakTool(pi: { exec: ExecFn }) {
	const binder = createBinder(pi.exec, {
		isAlive: async (state) =>
			sidecarComplete(state) &&
			(await cdpHttpAlive(pi.exec, state.cdp)) &&
			(await socketAlive(state.socket)),
	});

	return {
		name: "cloak",
		label: "Cloak",
		hidden: true as const,
		defaultInactive: true as const,
		loadMode: "discoverable" as const,
		description:
			"Bind a leased Cloak browser and drive the leased tab through the daemon socket. " +
			"action=bind {site|profile|scratch} once per job; then action=navigate|click|type|… . " +
			"tabs are tagged owned_by_me / owned_by / unowned. new_tab mints a lease. " +
			"switch_tab to someone else's tab peeks. " +
			"Do not run browserctl, do not pass a cdp url. " +
			"The browser stays up after yield. action=release only when the job is done.",
		parameters: {
			type: "object",
			required: ["action"],
			properties: {
				action: {
					type: "string",
					enum: ["bind", "release", ...DRIVE_ACTIONS],
					description: "bind / release a target lease, or a daemon drive op on the bound tab",
				},
				profile: { type: "string", description: "Registered profile name (bind)" },
				site: { type: "string", description: "Site key to resolve, e.g. github.com (bind)" },
				account: { type: "string", description: "Exact account label when site has account-scoped faces (bind)" },
				scratch: { type: "boolean", description: "Bind an ephemeral anonymous scratch (bind)" },
				url: { type: "string", description: "navigate / new_tab URL" },
				path: { type: "string", description: "screenshot / peek output path" },
				x: { type: "number", description: "click / scroll x" },
				y: { type: "number", description: "click / scroll y" },
				button: { type: "string", description: "click button (left|right|middle)" },
				clicks: { type: "number", description: "click count" },
				text: { type: "string", description: "type / fill text" },
				key: { type: "string", description: "press key (Enter, Tab, …)" },
				dy: { type: "number", description: "scroll delta y" },
				selector: { type: "string", description: "extract / fill / wait_for_element CSS selector" },
				attribute: { type: "string", description: "extract attribute name" },
				timeout: { type: "number", description: "wait_for_load / wait_for_element timeout seconds" },
				accept: { type: "boolean", description: "dialog accept (true) or dismiss (false)" },
				prompt_text: { type: "string", description: "dialog prompt response" },
				target_id: { type: "string", description: "switch_tab / close_tab destination (not your bound pin)" },
			},
		},
		async execute(_id: string, params: CloakParams, _onUpdate: unknown, ctx: SessionCtx) {
			const action = params?.action;
			if (!action) {
				return textResult("cloak requires action= (bind, release, or a drive op).", { isError: true });
			}

			if (action === "release") {
				const out = await binder.releaseState(ctx);
				return textResult(out.text, { isError: !out.ok });
			}

			if (action === "bind") {
				const out = await binder.bind(ctx, params);
				if (!out.ok) return textResult(out.text, { isError: true });
				return textResult(bindSummary(out.state, out.used, out.reused), {
					details: {
						targetId: out.state.targetId,
						worker: out.state.worker,
						reused: out.reused,
					},
				});
			}

			if (!DRIVE_SET[action]) {
				return textResult(`unknown cloak action=${action}`, { isError: true });
			}

			let state = binder.loadState(ctx);
			if (!sidecarComplete(state)) {
				return textResult(
					"cloak act without bind: no sidecar (lease/worker/socket/target). Call action=bind first. Do not connect to CDP.",
					{ isError: true },
				);
			}

			const pruned = await pruneHeldLeases(pi.exec, state);
			if (!sidecarComplete(pruned)) {
				binder.dropState(ctx);
				return textResult(
					"cloak act without bind: no live target lease left. Call action=bind first. Do not connect to CDP.",
					{ isError: true },
				);
			}
			state = pruned;
			binder.storeState(ctx, state);

			const req = buildDriveRequest(action, state, params);
			let result: DaemonResponse;
			try {
				const timeoutMs = action === "navigate" || action.startsWith("wait_for") ? 60_000 : 30_000;
				result = await sendToDaemon(state.socket, req, timeoutMs);
			} catch (e) {
				return textResult(
					e instanceof Error ? e.message : String(e),
					{ isError: true },
				);
			}
			const visible = redactForModel(result) as DaemonResponse;
			if (isDaemonError(result)) {
				if (result.code === "TARGET_LEASE_REQUIRED") {
					await binder.discardState(ctx);
				}
				const msg = visible.message || visible.error || JSON.stringify(visible);
				const code = result.code ? `[${result.code}] ` : "";
				return textResult(`${code}${msg}`, { isError: true, details: visible });
			}
			if (action === "new_tab" || action === "switch_tab" || action === "close_tab") {
				state = applyTabResult(state, action, result);
				binder.storeState(ctx, state);
			}
			const shown = action === "tabs" ? redactForModel(remintTabsOwnership(result, state)) as DaemonResponse : visible;
			return textResult(JSON.stringify(shown), { details: shown });
		},
		async onSession(event: { reason: string }, ctx: SessionCtx) {
			if (event.reason !== "shutdown") return;
			await binder.releaseState(ctx);
		},
	};
}
