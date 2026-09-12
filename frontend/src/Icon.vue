<script setup>
defineProps({ name: String, size: { default: 20 } })
const paths = {
  play: 'm8 5 11 7-11 7Z',
  save: 'M5 3h12l4 4v14H3V3h2Zm2 0v6h10V3M7 21v-8h10v8',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1Z',
  close: 'm6 6 12 12M6 18 18 6',
  refresh: 'M20 7v5h-5M20 12a8 8 0 1 0-2 5M20 12l-2-5',
  image: 'M3 3h18v18H3ZM3 17l6-6 5 5 3-3 4 4M15 7h.01',
  history: 'M3 4v5h5M3 9a9 9 0 1 1 0 7M12 7v5l4 2',
  plus: 'M12 4v16M4 12h16',
  share: 'M12 16V3m-5 5 5-5 5 5M5 12v8h14v-8',
  download: 'M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4',
  upload: 'M12 17V4m-5 5 5-5 5 5M4 17v4h16v-4',
  edit: 'm4 16 12-12 4 4L8 20H4ZM13 7l4 4',
  check: 'm4 12 5 5L20 6',
  trash: 'M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7',
  layout: 'M3 3h8v18H3ZM15 3h6v8h-6ZM15 15h6v6h-6Z',
  layers: 'm12 3 10 5-10 5L2 8Zm-9 10 9 5 9-5M3 18l9 5 9-5',
  arrow: 'm9 5 7 7-7 7',
  stop: 'M6 6h12v12H6Z',
  link: 'm9 15 6-6M8 17l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2-1 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0',
}
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name] || paths.image" :fill="name === 'play' ? 'currentColor' : 'none'" /></svg></template>
