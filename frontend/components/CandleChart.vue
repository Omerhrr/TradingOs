<!-- TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
     Pure-SVG candle chart with loop-signal markers. No chart library: the
     chart must render identically offline, inside a panel, with zero deps. -->
<script setup lang="ts">
import { computed } from 'vue'
import type { ChartCandle, ChartMarker } from '~/types/trading'

const props = withDefaults(defineProps<{
  candles: ChartCandle[]
  markers?: ChartMarker[]
  fastWindow?: number
  slowWindow?: number
}>(), {
  markers: () => [],
  fastWindow: 12,
  slowWindow: 26,
})

const WIDTH = 900
const HEIGHT = 300
const PAD = { top: 12, right: 56, bottom: 24, left: 10 }

const geometry = computed(() => {
  const candles = props.candles
  if (!candles.length) return null
  const slot = (WIDTH - PAD.left - PAD.right) / candles.length
  const lows = candles.map(candle => candle.low)
  const highs = candles.map(candle => candle.high)
  const rawMin = Math.min(...lows)
  const rawMax = Math.max(...highs)
  const pad = Math.max((rawMax - rawMin) * 0.06, rawMax * 0.001, 1e-9)
  const min = rawMin - pad
  const max = rawMax + pad
  const span = max - min
  const x = (index: number) => PAD.left + index * slot + slot / 2
  const y = (price: number) => PAD.top + (1 - (price - min) / span) * (HEIGHT - PAD.top - PAD.bottom)

  const gridLines = [0, 1, 2, 3].map(step => {
    const price = min + span * (0.18 + step * 0.213)
    return { y: y(price), price }
  })

  const candles2 = candles.map((candle, index) => {
    const up = candle.close >= candle.open
    const bodyTop = y(Math.max(candle.open, candle.close))
    const bodyBottom = y(Math.min(candle.open, candle.close))
    return {
      index,
      up,
      cx: x(index),
      wickX: x(index),
      wickY1: y(candle.high),
      wickY2: y(candle.low),
      bodyX: x(index) - Math.max(1.2, Math.min(slot * 0.55, 12)) / 2,
      bodyY: bodyTop,
      bodyW: Math.max(1.2, Math.min(slot * 0.55, 12)),
      bodyH: Math.max(1, bodyBottom - bodyTop),
      openTime: candle.open_time,
    }
  })

  // Reference EMA overlays, computed from the visible candles only.
  const ema = (windowSize: number) => {
    if (candles.length < windowSize) return null
    const multiplier = 2 / (windowSize + 1)
    let running = candles.slice(0, windowSize).reduce((sum, candle) => sum + candle.close, 0) / windowSize
    const points: string[] = []
    for (let index = windowSize - 1; index < candles.length; index += 1) {
      const candle = candles[index]
      if (index > windowSize - 1 && candle) {
        running = (candle.close - running) * multiplier + running
      }
      points.push(`${x(index).toFixed(1)},${y(running).toFixed(1)}`)
    }
    return points.join(' ')
  }
  const fastLine = ema(Math.max(2, props.fastWindow))
  const slowLine = ema(Math.max(props.fastWindow + 1, props.slowWindow))

  const byEpoch = new Map(props.candles.map((candle, index) => [Math.floor(new Date(candle.open_time).getTime() / 1000), index]))
  const markers = props.markers
    .map(marker => {
      const index = byEpoch.get(marker.candle_open_epoch)
      const anchor = index === undefined ? undefined : candles[index]
      if (index === undefined || !anchor) return null
      const call = marker.side === 'CALL'
      const tipY = call ? y(anchor.low) + 9 : y(anchor.high) - 9
      const baseY = call ? tipY + 8 : tipY - 8
      const rejected = marker.status === 'REJECTED' || marker.status === 'PROPOSED'
      return {
        ...marker,
        cx: x(index),
        tipY,
        baseY,
        call,
        rejected,
        label: `${marker.side} · ${marker.status} · #${marker.intent_id} · ${new Date(marker.candle_open_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`,
      }
    })
    .filter((marker): marker is NonNullable<typeof marker> => marker !== null)

  const timeLabels = candles2
    .filter((_, index) => index % Math.max(1, Math.floor(candles.length / 6)) === 0)
    .map(candle => ({ x: candle.cx, label: new Date(candle.openTime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }))

  const last = candles[candles.length - 1]
  if (!last) return null
  return {
    slot,
    gridLines,
    candles: candles2,
    markers,
    timeLabels,
    fastLine,
    slowLine,
    lastPrice: last.close,
    lastY: y(last.close),
    minLabel: min.toFixed(5),
    maxLabel: max.toFixed(5),
  }
})
</script>

<template>
  <div class="chart-stage">
    <svg v-if="geometry" class="chart-svg" :viewBox="`0 0 ${WIDTH} ${HEIGHT}`" role="img" aria-label="Candle chart with loop signal markers">
      <line
        v-for="(grid, index) in geometry.gridLines"
        :key="`grid-${index}`"
        :x1="PAD.left" :y1="grid.y" :x2="WIDTH - PAD.right" :y2="grid.y"
        stroke="rgba(235,232,223,.07)" stroke-width="1"
      />
      <text
        v-for="(grid, index) in geometry.gridLines"
        :key="`price-${index}`"
        :x="WIDTH - PAD.right + 6" :y="grid.y + 3"
        class="chart-text chart-text--quiet"
      >{{ grid.price.toFixed(4) }}</text>

      <g v-for="candle in geometry.candles" :key="`candle-${candle.index}`">
        <line
          :x1="candle.wickX" :y1="candle.wickY1" :x2="candle.wickX" :y2="candle.wickY2"
          :stroke="candle.up ? '#83bbb0' : '#cf6a5c'" stroke-width="1"
        />
        <rect
          :x="candle.bodyX" :y="candle.bodyY" :width="candle.bodyW" :height="candle.bodyH"
          :fill="candle.up ? 'rgba(131,187,176,.85)' : 'rgba(207,106,92,.85)'"
        />
      </g>

      <polyline v-if="geometry.slowLine" :points="geometry.slowLine" fill="none" stroke="rgba(131,187,176,.42)" stroke-width="1.2" stroke-dasharray="5 4" />
      <polyline v-if="geometry.fastLine" :points="geometry.fastLine" fill="none" stroke="rgba(199,154,74,.8)" stroke-width="1.2" />

      <g v-for="marker in geometry.markers" :key="`marker-${marker.intent_id}`">
        <title>{{ marker.label }}</title>
        <polygon
          :points="marker.call
            ? `${marker.cx},${marker.tipY} ${marker.cx - 4.5},${marker.baseY} ${marker.cx + 4.5},${marker.baseY}`
            : `${marker.cx},${marker.tipY} ${marker.cx - 4.5},${marker.baseY} ${marker.cx + 4.5},${marker.baseY}`"
          :fill="marker.call ? '#83bbb0' : '#cf6a5c'"
          :fill-opacity="marker.rejected ? 0.25 : 0.95"
          :stroke="marker.call ? '#83bbb0' : '#cf6a5c'"
          :stroke-opacity="marker.rejected ? 0.9 : 0"
          stroke-width="1"
        />
      </g>

      <line :x1="PAD.left" :y1="geometry.lastY" :x2="WIDTH - PAD.right" :y2="geometry.lastY" stroke="rgba(235,232,223,.22)" stroke-dasharray="2 4" stroke-width="1" />
      <text v-for="(tick, index) in geometry.timeLabels" :key="`time-${index}`" :x="tick.x" :y="HEIGHT - 6" class="chart-text chart-text--quiet" text-anchor="middle">{{ tick.label }}</text>
    </svg>
    <div v-else class="chart-empty">
      <span class="empty-glyph">▮</span>
      <p>No candles for this pair yet. The reconciler ingests them as soon as the practice broker is connected.</p>
    </div>
    <div v-if="geometry" class="chart-legend mono micro">
      <span class="legend-key"><span class="swatch swatch--call"></span>CALL SIGNAL</span>
      <span class="legend-key"><span class="swatch swatch--put"></span>PUT SIGNAL</span>
      <span class="legend-key"><span class="swatch swatch--hollow"></span>REJECTED BY RISK GATE</span>
      <span class="legend-key"><span class="swatch swatch--fast"></span>EMA {{ fastWindow }}</span>
      <span class="legend-key"><span class="swatch swatch--slow"></span>EMA {{ slowWindow }}</span>
    </div>
  </div>
</template>
