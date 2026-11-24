/**
 * 互動 —— 滑鼠拖曳 + 側邊欄 slider,雙向同步。
 *
 * 支援單軸與雙軸拖曳:
 *   axes: { x: {min,max} }              → 只能左右
 *   axes: { z: {min,max} }              → 只能前後
 *   axes: { x: {...}, z: {...} }        → 兩軸自由移動,各自夾範圍
 *
 * 拖曳的作法:
 *   1. mousedown 用 raycaster 找出點到哪個可拖曳 Group
 *   2. 建立一個**水平**平面(通過物件、法線朝上)
 *      —— 對地面上的東西最直覺;純垂直軸的物件則改用面向相機的平面
 *   3. mousemove 把射線打到平面取交點,只取允許的軸並夾住範圍
 *
 * 為什麼用平面而不是滑鼠 delta:透視投影下 delta 換算會失真,
 * 打平面取交點不管相機怎麼轉都會精準跟著游標。
 */
import * as THREE from 'three';
import { clamp, CONFIG as C } from './config.js';

export function createInteraction({ renderer, camera, controls, targets, onChange }) {
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const plane = new THREE.Plane();
  const hit = new THREE.Vector3();
  const offset = new THREE.Vector3();

  let dragging = null;
  let hovered = null;
  //: 即時模式要把拖曳整個關掉 —— 3D 畫的是實機的位置,用滑鼠去推它只會
  //  騙自己。關掉之後 OrbitControls 照常,所以還是轉得動視角。
  let enabled = true;

  const byKey = Object.fromEntries(targets.map((t) => [t.key, t]));

  /** 目前每個目標的邏輯位置(不一定等於 group.position,例如 HATS 的 X 是子層) */
  const values = {};
  for (const t of targets) {
    values[t.key] = {};
    for (const ax of Object.keys(t.axes)) values[t.key][ax] = t.axes[ax].default ?? 0;
  }

  function applyValue(t, axis, v) {
    values[t.key][axis] = v;
    // 有些目標的某一軸要套用到子物件(例如 HATS 的橫移是上層滑塊)
    if (t.apply?.[axis]) t.apply[axis](t.group, v);
    else t.group.position[axis] = v;
  }

  function updatePointer(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    pointer.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
    pointer.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
  }

  function pick() {
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(targets.map((t) => t.group), true);
    if (!hits.length) return null;
    let o = hits[0].object;
    while (o && !o.userData?.draggable) o = o.parent;
    return o ? byKey[o.userData.draggable] : null;
  }

  function setHighlight(t, on) {
    if (!t) return;
    t.group.traverse((o) => {
      if (!o.isMesh || !o.material?.emissive) return;
      if (on) {
        if (o.userData._emi === undefined) o.userData._emi = o.material.emissive.getHex();
        o.material.emissive.setHex(C.highlight);
      } else if (o.userData._emi !== undefined) {
        o.material.emissive.setHex(o.userData._emi);
      }
    });
  }

  /** 建立拖曳平面:地面物件用水平面,只有 Y 軸的用面向相機的平面 */
  function makePlane(t) {
    const anchor = new THREE.Vector3(
      values[t.key].x ?? t.group.position.x,
      t.planeY ?? t.group.position.y,
      values[t.key].z ?? t.group.position.z
    );
    plane.setFromNormalAndCoplanarPoint(new THREE.Vector3(0, 1, 0), anchor);
  }

  function onDown(ev) {
    if (!enabled || ev.button !== 0) return;
    updatePointer(ev);
    const t = pick();
    if (!t) return;

    dragging = t;
    controls.enabled = false;                 // 拖物件時不要同時轉鏡頭
    renderer.domElement.style.cursor = 'grabbing';

    makePlane(t);
    raycaster.setFromCamera(pointer, camera);
    if (raycaster.ray.intersectPlane(plane, hit)) {
      offset.set(
        (values[t.key].x ?? 0) - hit.x,
        0,
        (values[t.key].z ?? 0) - hit.z
      );
    } else {
      offset.set(0, 0, 0);
    }
    ev.preventDefault();
  }

  function onMove(ev) {
    if (!enabled) return;
    updatePointer(ev);

    if (!dragging) {
      const t = pick();
      if (t !== hovered) {
        setHighlight(hovered, false);
        hovered = t;
        setHighlight(hovered, true);
        renderer.domElement.style.cursor = t ? 'grab' : 'default';
      }
      return;
    }

    raycaster.setFromCamera(pointer, camera);
    if (!raycaster.ray.intersectPlane(plane, hit)) return;

    for (const axis of Object.keys(dragging.axes)) {
      if (axis !== 'x' && axis !== 'z') continue;      // Y 不用拖,由 slider 控
      const range = dragging.axes[axis];
      const raw = hit[axis] + offset[axis];
      const v = clamp(raw, range.min, range.max);
      applyValue(dragging, axis, v);
      onChange?.(dragging.key, axis, v, 'drag');
    }
  }

  function onUp() {
    if (!dragging) return;
    controls.enabled = true;
    renderer.domElement.style.cursor = hovered ? 'grab' : 'default';
    dragging = null;
  }

  const el = renderer.domElement;
  el.addEventListener('pointerdown', onDown);
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);

  // 初始化到預設值
  for (const t of targets) {
    for (const ax of Object.keys(t.axes)) applyValue(t, ax, values[t.key][ax]);
  }

  return {
    /** 由 slider 設定 */
    set(key, axis, value) {
      const t = byKey[key];
      if (!t || !t.axes[axis]) return;
      const v = clamp(value, t.axes[axis].min, t.axes[axis].max);
      applyValue(t, axis, v);
      onChange?.(key, axis, v, 'slider');
    },
    get(key, axis) { return values[key]?.[axis]; },
    /** 開關拖曳。關掉時順手把 hover 的高亮與游標清掉。 */
    setEnabled(v) {
      enabled = !!v;
      if (!enabled) {
        setHighlight(hovered, false);
        hovered = null;
        dragging = null;
        controls.enabled = true;
        renderer.domElement.style.cursor = 'default';
      }
    },
    dispose() {
      el.removeEventListener('pointerdown', onDown);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    },
  };
}
