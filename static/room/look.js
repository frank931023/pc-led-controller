/**
 * 聲學測試室 —— 外觀
 *
 * 顏色、透明度、燈光、相機。**跟實體尺寸無關**,改這裡不會影響任何量測意義。
 * 要改尺寸請去 spec.js。
 *
 * 結構刻意跟 spec.js 對齊,config.js 會把兩邊深層合併成一個 CONFIG。
 */

export const LOOK = {
  room: {
    color: 0x9aa3ad, opacity: 0.16,
    floorColor: 0x7d858e, floorOpacity: 0.55,
  },

  speakerRail: { color: 0xd8dde3, endColor: 0xaeb5bd },
  speakerStand: {
    base: { color: 0x9aa1a9 },
    post: { color: 0xc0c6cd },
  },

  speakerArray: {
    speaker: { color: 0x24282e, coneColor: 0x15181c },
    frame: { color: 0x3a4048 },
  },

  screen: {
    panelColor: 0x14171b,
    frameColor: 0x3a4048,
    standColor: 0xc0c6cd,
    baseColor: 0x9aa1a9,
    footColor: 0x8a9199,
    mountColor: 0x6f767e,
    emissive: 0x0a1622,          // 一點自發光,看起來像開著
    emissiveIntensity: 0.55,
  },

  floorRailZ: { color: 0xd8dde3, tieColor: 0xc8ced5 },
  floorRailX: { color: 0xc8ced5, capColor: 0xaeb5bd },

  hats: {
    torso: { color: 0xe8e4de },
    head: { color: 0xf0ece6 },
    ear: { color: 0x3a3f46 },
    mouth: { color: 0x8a9099 },
    mrpMarker: { color: 0xf85149 },
    stand: { color: 0xd0d6dc },
    holder: { color: 0xb6bdc4 },
    bogie: { color: 0x9aa1a9 },
    platform: { color: 0xe4e8ec },
    turntable: { color: 0xb9c0c8 },
  },

  tables: { color: 0xeceff2, columnColor: 0xc0c6cd, baseColor: 0xb0b7be },

  micRig: {
    bridge: { color: 0xd8dde3, capColor: 0xaeb5bd },
    trolley: { color: 0x8f979f },
    rod: { color: 0xbfc6cd },
    mic: { color: 0xd8dde0, capsuleColor: 0x2f343b },
  },


  /** 滑鼠移過去時的高亮色 */
  highlight: 0x009b85,

  /** 場景背景 */
  background: 0x11151a,

  /** 地面格線 */
  grid: { divisions: 20, color1: 0x3a424c, color2: 0x252b32, opacity: 0.42 },

  camera: {
    fov: 50, near: 0.05, far: 100,
    position: [4.2, 3.0, 5.4],
    target: [0, 1.0, 0],
    minDistance: 1.0, maxDistance: 24,
  },

  lights: {
    hemi: { sky: 0xffffff, ground: 0x60666e, intensity: 0.85 },
    key: { color: 0xffffff, intensity: 1.1, position: [3.5, 4.5, 3.0],
           shadowMapSize: 1024, shadowSpan: 4 },
    fill: { color: 0xffffff, intensity: 0.35, position: [-3, 2.5, -2.5] },
  },
};
