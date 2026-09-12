<script setup>
import Studio from './Studio.vue'
defineProps({ api: Object, initialConfig: { type: Object, default: () => ({}) } })
defineEmits(['save', 'switch', 'close'])
</script>
<template><Studio :api="api" :initial-config="initialConfig" settings @save="config => $emit('save', config)" @switch="$emit('switch')" @close="$emit('close')" /></template>
