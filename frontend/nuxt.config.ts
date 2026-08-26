// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
export default defineNuxtConfig({
  compatibilityDate: '2026-08-26',
  devtools: { enabled: true },
  css: ['~/assets/css/main.css'],
  modules: ['@nuxtjs/color-mode'],
  colorMode: { preference: 'dark', fallback: 'dark', classSuffix: '' },
  runtimeConfig: {
    public: {
      apiBaseUrl: process.env.NUXT_PUBLIC_API_BASE_URL || 'http://localhost:8000/api/v1',
    },
  },
  nitro: {
    devProxy: {
      '/manus-storage': {
        target: 'http://localhost:3000/manus-storage',
        changeOrigin: true,
      },
    },
  },
  app: {
    head: {
      title: 'TradingOS · Practice Control Plane',
      link: [
        { rel: 'icon', type: 'image/png', href: '/manus-storage/tradingos-symbol_07b6b184.png' },
      ],
      meta: [
        { name: 'description', content: 'A practice-first control plane for bounded autonomous trading operations.' },
      ],
    },
  },
})
