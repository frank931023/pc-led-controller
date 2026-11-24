/**
 * Saved setups: "where every object in the room stands for one measurement".
 *
 * Only numbers are stored, never 3D objects: a setup is {axis id: value}. The ids are the
 * axis table's (mcc/room.json), so a saved setup can be sent to the rig unchanged.
 * Renaming an id breaks the setups saved with it. A setup also carries the overlays drawn with
 * it (circles, points, distances; see overlays.js). Examples (readonly) cannot be overwritten.
 */
import { confirmBox, formBox, toast, esc, jsonFetch, isOk, errorText } from './ui.js';

const API = '/api/room/setups';

export const listSetups = () => jsonFetch(API).then((d) => d.setups || []);

function send(url, method, body) {
  return jsonFetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** Save what the 3D view shows. Asks for a name first: unnamed setups are unrecognisable a week later. */
export async function saveCurrent(axes, { existing = null, overlays = undefined } = {}) {
  const n = Object.keys(axes).length;
  const v = await formBox({
    title: existing ? 'Update this setup' : 'Save the current setup',
    note: `Saves the position of <b>${n}</b> axes.`
      + (existing ? '' : ' You can load it again later, or apply it to the rig.'),
    fields: [
      { key: 'name', label: 'Name', required: true, value: existing?.name || '',
        placeholder: 'e.g. Near field 0.5 m · HATS head 17°' },
      { key: 'description', label: 'Description', textarea: true,
        value: existing?.description || '',
        placeholder: 'Which test it is for, and why it is set up this way' },
    ],
    ok: existing ? 'Update' : 'Save',
  });
  if (!v) return null;

  const d = await send(existing ? `${API}/${encodeURIComponent(existing.id)}` : API,
    'POST', { name: v.name, description: v.description, axes, overlays, source: 'setup' });
  if (!isOk(d)) { toast(errorText(d, 'Could not save'), 'bad'); return null; }
  toast(`Saved "${d.setup.name}"`);
  return d.setup;
}

/** Save where the rig is now (not what the sliders show). Marked as "live" in the list. */
export async function saveFromLive(axes, overlays = undefined) {
  const v = await formBox({
    title: 'Save the rig position as a setup',
    note: 'Saves <b>the positions reported by the rig now</b>, not the slider values.',
    fields: [
      { key: 'name', label: 'Name', required: true, placeholder: 'e.g. 2026-11-24 validation' },
      { key: 'description', label: 'Description', textarea: true },
    ],
  });
  if (!v) return null;
  const d = await send(API, 'POST', { name: v.name, description: v.description, axes, overlays, source: 'live' });
  if (!isOk(d)) { toast(errorText(d, 'Could not save'), 'bad'); return null; }
  toast(`Saved "${d.setup.name}"`);
  return d.setup;
}

export async function removeSetup(s) {
  const yes = await confirmBox({
    title: 'Delete this setup?',
    html: `<div class="bxnote"><b>${esc(s.name)}</b><br>${esc(s.description || '(no description)')}</div>
      <div class="bxnote warn">This cannot be undone. It does not move anything.</div>`,
    ok: 'Delete', danger: true,
  });
  if (!yes) return false;
  const d = await send(`${API}/${encodeURIComponent(s.id)}`, 'DELETE');
  toast(isOk(d) ? 'Deleted' : errorText(d, 'Could not delete'), isOk(d) ? 'ok' : 'bad');
  return isOk(d);
}

/**
 * Put a setup into the 3D view (the view only, nothing moves).
 * Missing axes are left where they are: an old setup may predate an axis, and filling in a
 * default would look like it had been measured.
 */
export function applyToScene(setup, controls) {
  const applied = [];
  const missing = [];
  for (const [id, v] of Object.entries(setup.axes || {})) {
    const c = controls[id];
    if (!c) { missing.push(id); continue; }
    c.set(v);
    applied.push(id);
  }
  const absent = Object.keys(controls).filter((id) => !(id in (setup.axes || {})));
  return { applied, missing, absent };
}
