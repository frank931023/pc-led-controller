/**
 * 聲學測試室 —— 場景設定(組裝層)
 *
 * 這個檔案本身**沒有任何數字**,只負責把兩份資料合起來:
 *
 *     spec.js   ⭐ 規格尺寸 —— 要改尺寸改這裡
 *     look.js      顏色 / 材質 / 燈光 / 相機 —— 要改外觀改這裡
 *              ↓
 *     CONFIG       builders.js 與 main.js 讀這個
 *
 * 分成兩份的理由:規格數字會拿去對現場、會被人質疑「這 0.42 哪來的」,
 * 所以要能單獨拿出來看。顏色不會。混在一起的話,改尺寸時要在
 * 一堆 0x9aa3ad 裡面找 0.42,很容易改錯。
 */

import { SPEC } from './spec.js';
import { LOOK } from './look.js';

const isPlain = (v) => v && typeof v === 'object' && !Array.isArray(v);

/** 深層合併:同名的物件往下併,其他直接覆蓋(陣列整個換掉) */
function merge(base, over) {
  const out = { ...base };
  for (const k of Object.keys(over)) {
    out[k] = isPlain(over[k]) && isPlain(base[k]) ? merge(base[k], over[k]) : over[k];
  }
  return out;
}

export const CONFIG = merge(SPEC, LOOK);

export const clamp = (v, min, max) => Math.min(max, Math.max(min, v));

// 也把原始的兩份放出去,萬一要單獨用(例如只想匯出規格)
export { SPEC, LOOK };
