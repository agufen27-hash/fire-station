-- 'PROTECCION_RESPIRATORIA' (23) no entraba en varchar(20). La vista depende de la columna.
drop view public.vw_inventario_cuartel;
alter table public.inventario_cuartel alter column categoria type varchar(30);
create view public.vw_inventario_cuartel
with (security_invoker = true) as
select i.*,
       public.nombre_legible(c.nombre_completo) as creado_por,
       public.nombre_legible(m.nombre_completo) as modificado_por
from public.inventario_cuartel i
left join public.personal_cloud c on c.id = i.creado_por_id
left join public.personal_cloud m on m.id = i.modificado_por_id;
grant select on public.vw_inventario_cuartel to service_role;
revoke all on public.vw_inventario_cuartel from anon, authenticated;

-- Texto demasiado largo (varchar) -> rechazo con motivo, no un error 500.
create or replace function public.rpc_inventario_guardar(p_operador_id integer, p_id uuid, p_datos jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_motivo text := public.inventario_operador_motivo(p_operador_id);
  v_codigo text := nullif(upper(trim(coalesce(p_datos->>'codigo_qr', ''))), '');
  v_fila   public.inventario_cuartel;
begin
  if v_motivo is not null then
    return jsonb_build_object('ok', false, 'motivo', v_motivo);
  end if;
  if length(trim(coalesce(p_datos->>'nombre', ''))) = 0
     or length(trim(coalesce(p_datos->>'ubicacion', ''))) = 0
     or coalesce(p_datos->>'categoria', '') = '' then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INCOMPLETOS');
  end if;

  if p_id is null then
    insert into public.inventario_cuartel
      (codigo_qr, nombre, categoria, ubicacion, estado, marca_modelo, numero_serie, observaciones,
       creado_por_id, modificado_por_id)
    values
      (coalesce(v_codigo, 'FS-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8))),
       trim(p_datos->>'nombre'), p_datos->>'categoria', trim(p_datos->>'ubicacion'),
       coalesce(nullif(p_datos->>'estado', ''), 'OPERATIVO'),
       nullif(trim(coalesce(p_datos->>'marca_modelo', '')), ''),
       nullif(trim(coalesce(p_datos->>'numero_serie', '')), ''),
       nullif(trim(coalesce(p_datos->>'observaciones', '')), ''),
       p_operador_id, p_operador_id)
    returning * into v_fila;
  else
    update public.inventario_cuartel set
      codigo_qr         = coalesce(v_codigo, codigo_qr),
      nombre            = trim(p_datos->>'nombre'),
      categoria         = p_datos->>'categoria',
      ubicacion         = trim(p_datos->>'ubicacion'),
      estado            = coalesce(nullif(p_datos->>'estado', ''), estado),
      marca_modelo      = nullif(trim(coalesce(p_datos->>'marca_modelo', '')), ''),
      numero_serie      = nullif(trim(coalesce(p_datos->>'numero_serie', '')), ''),
      observaciones     = nullif(trim(coalesce(p_datos->>'observaciones', '')), ''),
      modificado_por_id = p_operador_id
    where id = p_id and activo
    returning * into v_fila;
    if not found then
      return jsonb_build_object('ok', false, 'motivo', 'ITEM_INEXISTENTE_O_DADO_DE_BAJA');
    end if;
  end if;
  return jsonb_build_object('ok', true, 'item', to_jsonb(v_fila));
exception
  when unique_violation then
    return jsonb_build_object('ok', false, 'motivo', 'CODIGO_QR_DUPLICADO');
  when check_violation or string_data_right_truncation then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INVALIDOS', 'detalle', sqlerrm);
end
$$;
revoke all on function public.rpc_inventario_guardar(integer, uuid, jsonb) from public, anon, authenticated;
grant execute on function public.rpc_inventario_guardar(integer, uuid, jsonb) to service_role;
