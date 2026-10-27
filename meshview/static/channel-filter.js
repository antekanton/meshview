/* Channel filter options for a <select>, from /api/channels.
 *
 * Options come busiest first as "Name (count)". The value "?" stands for all unknown
 * channels together and is shown as unknownText. Existing options (e.g. "All channels")
 * are kept. params are passed to /api/channels, e.g. {portnum: 1} or {source: "nodes"}.
 */
const UNKNOWN_CHANNEL_GROUP = "?";

async function loadChannelOptions(select, params = {}, unknownText = "Unknown") {
    try {
        const url = new URL("/api/channels", window.location.origin);
        for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);

        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();

        for (const { channel, count } of data.options || []) {
            const option = document.createElement("option");
            option.value = channel;
            option.dataset.count = count;
            const name = channel === UNKNOWN_CHANNEL_GROUP ? unknownText : channel;
            option.textContent = `${name} (${count})`;
            select.appendChild(option);
        }
    } catch (err) {
        console.error("Failed loading channels:", err);
    }
}


/* Relabel the unknown-channels option once translations arrive after the list. */
function setUnknownChannelsText(select, unknownText) {
    const option = select.querySelector(`option[value="${UNKNOWN_CHANNEL_GROUP}"]`);
    if (option && unknownText) option.textContent = `${unknownText} (${option.dataset.count})`;
}
