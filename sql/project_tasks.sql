select 
  tf.project_id, 
  pf.project_name, 
  tw.wbs,
  tf.task_id, 
  task_name, 
  tf.start_date, 
  tf.start_date_actual, 
  tf.due_date, 
  tf.due_date_actual,
  calculated_planned, 
  progress 
from task_fact tf
left join task_wbs tw on tw.project_id = tf.project_id and tw.task_id = tf.task_id
inner join project_fact pf on pf.project_id = tf.project_id
where tf.project_id in (1186533, 1325524, 1325527)
  and private = false
order by 
  tf.project_id desc,
  tw.display_order;
