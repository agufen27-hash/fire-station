-- Coordenadas exactas del Cuartel Central (Adelia María). Reemplaza las estimadas.
update public.zonas_geocerca
   set ubicacion = st_setsrid(st_makepoint(-64.016564, -33.631258), 4326)::geography,
       radio_tolerancia_metros = 150
 where nombre = 'Cuartel Central';
