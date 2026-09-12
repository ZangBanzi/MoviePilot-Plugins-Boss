<script setup>
import Studio from './Studio.vue'
defineProps({ api: Object })
defineEmits(['switch', 'close', 'action'])
</script>
<template><Studio :api="api" @switch="$emit('switch')" @close="$emit('close')" @action="$emit('action')" /></template>
