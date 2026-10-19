// Packet reach: the number of distinct gateways that heard a packet,
// shown against the number of active gateways (reception in the last 24h).
//
// Markup: <span class="reach" data-packet-id="…" data-import-time-us="…"></span>
// Pages set window.REACH_TITLE to a translated "{reach} … {active}" template.

// Gateways keep reporting a packet for a while after it first shows up.
const REACH_REFRESH_WINDOW_US = 5 * 60 * 1_000_000;
const REACH_MAX_IDS = 100;
const REACH_TITLE_DEFAULT = "Reach: {reach} of {active} active gateways (last 24h)";

function reachTitle(reach, activeGateways) {
    return (window.REACH_TITLE || REACH_TITLE_DEFAULT)
        .replace("{reach}", reach ?? 0)
        .replace("{active}", activeGateways ?? "?");
}

function reachHtml(pkt, activeGateways) {
    const reach = pkt.reach ?? 0;
    return `<span class="reach"
                  data-packet-id="${pkt.id}"
                  data-import-time-us="${pkt.import_time_us || 0}"
                  title="${reachTitle(reach, activeGateways)}">${reach}/${activeGateways ?? "?"}</span>`;
}

function setReach(el, reach, activeGateways) {
    el.textContent = `${reach}/${activeGateways ?? "?"}`;
    el.title = reachTitle(reach, activeGateways);
}

async function refreshRecentReach(root = document) {
    const cutoffUs = Date.now() * 1000 - REACH_REFRESH_WINDOW_US;
    const elements = [...root.querySelectorAll(".reach[data-packet-id]")].filter(
        el => Number(el.dataset.importTimeUs) >= cutoffUs
    );
    if (!elements.length) return;

    // Rows are rendered newest first, so the cap keeps the freshest packets.
    const ids = [...new Set(elements.map(el => el.dataset.packetId))].slice(0, REACH_MAX_IDS);

    try {
        const res = await fetch(`/api/packets/reach?ids=${ids.join(",")}`);
        if (!res.ok) return;
        const data = await res.json();

        for (const el of elements) {
            const reach = data.reach?.[el.dataset.packetId];
            if (reach !== undefined) setReach(el, reach, data.active_gateways);
        }
    } catch (err) {
        console.error("Reach refresh failed:", err);
    }
}
