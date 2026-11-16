<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/reconcile')
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <div>
    <h1>交班对账</h1>
    <p class="lead">
      计划级并集校验物化落档：各段清单并集须等于发起快照、段间键不交；
      计划成功当且仅当每段成功且并集完整（非各段成功计数加总）。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>报告</th><th>计划</th><th>阶段</th><th>快照键数</th><th>并集键数</th>
            <th>缺失</th><th>多出</th><th>相交</th><th>并集完整</th><th>每段成功</th><th>计划成功</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>#{{ r.id }}</td>
            <td>#{{ r.plan_id }}</td>
            <td>{{ r.stage }}</td>
            <td>{{ r.snapshot_count }}</td>
            <td>{{ r.union_count }}</td>
            <td>{{ r.missing_count }}</td>
            <td>{{ r.extra_count }}</td>
            <td>{{ r.overlap_count }}</td>
            <td><span class="tag" :class="r.union_ok ? 'on' : 'bad'">{{ r.union_ok ? '完整' : '缺损' }}</span></td>
            <td><span class="tag" :class="r.all_segments_ok ? 'on' : 'bad'">{{ r.all_segments_ok ? '是' : '否' }}</span></td>
            <td><span class="tag" :class="r.plan_ok ? 'on' : 'bad'">{{ r.plan_ok ? '一致' : '不一致' }}</span></td>
            <td>{{ r.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
