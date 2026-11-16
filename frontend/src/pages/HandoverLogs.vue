<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/handovers')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function unionCell(h) {
  if (h.latest_union_ok === null || h.latest_union_ok === undefined) return '—'
  return h.latest_union_ok ? '并集完整' : '并集缺损'
}
</script>

<template>
  <div>
    <h1>交班记录</h1>
    <p class="lead">每次交班为一个计划，下挂有序段链条：双方班次、分段上限、段进度、并集校验结论。</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>计划</th><th>交班方</th><th>接班方</th><th>快照键数</th><th>每段上限</th>
            <th>段进度</th><th>并集校验</th><th>状态</th><th>开始</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="h in rows" :key="h.id">
            <td>#{{ h.id }}</td>
            <td>{{ h.from_shift_name }}</td>
            <td>{{ h.to_shift_name }}</td>
            <td>{{ h.batch_count }}</td>
            <td>{{ h.segment_size }}</td>
            <td>{{ h.done_segments }}/{{ h.segment_count }}</td>
            <td>{{ unionCell(h) }}</td>
            <td>
              <span class="tag" :class="h.status === '已完成' ? 'on' : h.status === '失败' ? 'bad' : ''">{{ h.status }}</span>
            </td>
            <td>{{ h.started_at }}</td>
            <td>{{ h.finished_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
