import { createApp } from 'vue'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import 'vuetify/styles'
import Host from './Host.vue'
createApp(Host).use(createVuetify({components, directives, theme:{defaultTheme:'dark'}})).mount('#app')
