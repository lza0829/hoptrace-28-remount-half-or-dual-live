<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const err = ref('')
const gone = ref(null)

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  gone.value = null
  detail.value = null
  try {
    detail.value = await api.timeline(id)
  } catch (e) {
    // 旧键已重挂：后端 410，响应体 detail.remounted_to 指向承接键
    try {
      const body = JSON.parse(e.message)
      if (body.detail?.remounted_to) {
        gone.value = body.detail
        return
      }
      err.value = body.detail?.message || String(e.message)
    } catch (_) {
      err.value = String(e.message || e)
    }
  }
}

function followRemount() {
  if (!gone.value?.remounted_to) return
  selected.value = gone.value.remounted_to
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, (id) => {
  loadDetail(id).catch((e) => { err.value = String(e.message || e) })
})

const hops = computed(() => detail.value?.hops || [])
const proof = computed(() => detail.value?.proof)
</script>

<template>
  <div>
    <p v-if="err" class="muted">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.waybills_retired ?? 0 }}</b><span>已重挂旧键</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">{{ w.id }}</span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>
        <div class="v-axis" v-if="hops.length">
          <div class="hop-node" v-for="h in hops" :key="h.id">
            <div class="ts">{{ h.event_ts }}</div>
            <div class="code">{{ h.event_code }}</div>
            <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
          </div>
        </div>
        <div v-else-if="gone" class="gone-card">
          <h3>该运单键已重挂</h3>
          <p class="muted">{{ gone.message }}</p>
          <dl class="kv">
            <dt>承接键</dt><dd>{{ gone.remounted_to }}</dd>
            <dt>迁移边</dt><dd class="mono">#{{ gone.edge_id }} · {{ gone.edge_uid }}</dd>
          </dl>
          <button class="btn" @click="followRemount">沿迁移边查看 {{ gone.remounted_to }} 全序</button>
        </div>
        <p v-else class="muted">无正式轨迹点</p>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>
        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末 hop</dt><dd>{{ proof.last_hop_id }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">HOP</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot on"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
          <!-- pad-28 continuity -->
</template>
        <template v-if="!hops.length">
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
        </template>
      </footer>
    </div>
  </div>
</template>
