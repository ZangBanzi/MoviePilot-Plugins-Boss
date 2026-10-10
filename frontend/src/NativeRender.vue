<script setup>
// Mirrors MoviePilot PageRender/FormRender's component/content/model/action contract.
import { computed } from 'vue'
const props = defineProps({ config: Object, model: Object })
const emit = defineEmits(['action'])
const bindings = computed(() => {
  const result = {...props.config?.props}
  if (result.model && props.model) result.modelValue = props.model[result.model]
  delete result.model
  return result
})
const events = computed(() => {
  const result = {}
  if (props.config?.props?.model && props.model)
    result['update:modelValue'] = value => { props.model[props.config.props.model] = value }
  for (const [key, value] of Object.entries(props.config?.events || {}))
    result[key] = () => emit('action', value)
  return result
})
</script>
<template><component :is="config.component" v-bind="bindings" v-on="events">{{ config.text }}<NativeRender v-for="(item, index) in config.content || []" :key="index" :config="item" :model="model" @action="emit('action', $event)" /></component></template>
