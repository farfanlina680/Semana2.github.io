import os
import shutil
import subprocess
import logging
import json
import time
import stat
import threading
from datetime import datetime
from flask import Flask, render_template, request, session, redirect, url_for, jsonify

app = Flask(__name__)
app.secret_key = 'supersecretkey'
logging.basicConfig(level=logging.INFO)

# Almacenamiento global para progreso y resultados
global_progress = []
global_results = []
global_backup_path = ""
despliegue_activo = False
despliegue_completado_global = False

# Función para obtener sitios y aplicaciones de IIS
def get_iis_sites():
    try:
        ps_script = """
        Import-Module WebAdministration
        $sites = @{}
        Get-WebSite | ForEach-Object {
            $siteName = $_.Name
            $apps = @()
            Get-WebApplication -Site $siteName | ForEach-Object {
                $path = $_.Path
                if ($path -ne '/') {
                    $apps += $path.Split('/')[-1]
                }
            }
            $sites[$siteName] = $apps
        }
        $sites | ConvertTo-Json -Depth 5
        """
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=30
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        return {}
    except Exception as e:
        logging.error(f"Error obteniendo sitios IIS: {str(e)}")
        return {}

# Función para obtener la ruta física de un sitio o aplicación
def obtener_ruta_fisica(sitio, app_path=''):
    try:
        ps_script = f"""
        Import-Module WebAdministration
        $appPath = 'IIS:/Sites/{sitio}'
        if ('{app_path}' -ne '') {{
            $appPath += '/{app_path}'
        }}
        (Get-Item $appPath).PhysicalPath
        """
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception as e:
        logging.error(f"Error obteniendo ruta física: {str(e)}")
        return None

# Función para obtener el estado de un sitio
def obtener_estado_sitio(sitio):
    try:
        ps_script = f"(Get-Website -Name '{sitio}').State"
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        return result.stdout.strip()
    except Exception as e:
        logging.error(f"Error obteniendo estado del sitio {sitio}: {str(e)}")
        return "Error"

# Función para obtener el estado de un pool
def obtener_estado_pool(pool_name):
    try:
        ps_script = f"(Get-WebAppPoolState -Name '{pool_name}').Value"
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        estado = result.stdout.strip()
        return estado if estado else "Stopped"  # Valor por defecto si está vacío
    except Exception as e:
        logging.error(f"Error obteniendo estado del pool {pool_name}: {str(e)}")
        return "Error"

# Función para obtener el nombre del pool de un sitio
def obtener_pool_del_sitio(sitio):
    try:
        ps_script = f"(Get-Website -Name '{sitio}').applicationPool"
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        pool_name = result.stdout.strip()
        return pool_name if pool_name else f"{sitio}_Pool"  # Valor por defecto si está vacío
    except Exception as e:
        logging.error(f"Error obteniendo pool del sitio {sitio}: {str(e)}")
        return f"{sitio}_Pool"

# Función para hacer backup manteniendo la estructura de directorios
def hacer_backup(ruta_origen, ruta_destino_base, ruta_relativa):
    try:
        if not os.path.exists(ruta_origen):
            return False, f"Ruta origen no existe: {ruta_origen}"
        # Construir ruta destino completa
        ruta_destino = os.path.join(ruta_destino_base, ruta_relativa)
        os.makedirs(os.path.dirname(ruta_destino), exist_ok=True)
        # Si es un directorio, copiamos todo su contenido
        if os.path.isdir(ruta_origen):
            shutil.copytree(ruta_origen, ruta_destino, dirs_exist_ok=True)
        else:
            shutil.copy2(ruta_origen, ruta_destino)
        return True, f"Backup exitoso en {ruta_destino}"
    except Exception as e:
        return False, f"Error en backup: {str(e)}"

# Función para detener un pool si está iniciado
def detener_pool(pool_name):
    try:
        estado = obtener_estado_pool(pool_name)
        if estado == "Started":
            stop_pool_script = f"Stop-WebAppPool -Name '{pool_name}'"
            subprocess.run(["powershell", "-Command", stop_pool_script], shell=True, check=True)
            return True, f"Pool detenido: {pool_name}"
        return True, f"Pool ya estaba detenido: {pool_name}"
    except Exception as e:
        return False, f"Error deteniendo pool {pool_name}: {str(e)}"

# Función para iniciar un pool si estaba iniciado originalmente
def iniciar_pool(pool_name, estado_original):
    try:
        estado_actual = obtener_estado_pool(pool_name)
        if estado_original == "Started" and estado_actual != "Started":
            start_pool_script = f"Start-WebAppPool -Name '{pool_name}'"
            subprocess.run(["powershell", "-Command", start_pool_script], shell=True, check=True)
            return True, f"Pool iniciado: {pool_name}"
        return True, f"Pool se mantuvo como estaba: {pool_name} ({estado_actual})"
    except Exception as e:
        return False, f"Error iniciando pool {pool_name}: {str(e)}"

# Función para detener un sitio si está iniciado
def detener_sitio(sitio):
    try:
        estado = obtener_estado_sitio(sitio)
        if estado == "Started":
            stop_site_script = f"Stop-WebSite -Name '{sitio}'"
            subprocess.run(["powershell", "-Command", stop_site_script], shell=True, check=True)
            return True, f"Sitio detenido: {sitio}"
        return True, f"Sitio ya estaba detenido: {sitio}"
    except Exception as e:
        return False, f"Error deteniendo sitio {sitio}: {str(e)}"

# Función para iniciar un sitio si estaba iniciado originalmente
def iniciar_sitio(sitio, estado_original):
    try:
        estado_actual = obtener_estado_sitio(sitio)
        if estado_original == "Started" and estado_actual != "Started":
            start_site_script = f"Start-WebSite -Name '{sitio}'"
            subprocess.run(["powershell", "-Command", start_site_script], shell=True, check=True)
            return True, f"Sitio iniciado: {sitio}"
        return True, f"Sitio se mantuvo como estaba: {sitio} ({estado_actual})"
    except Exception as e:
        return False, f"Error iniciando sitio {sitio}: {str(e)}"

# Función auxiliar para manejar archivos bloqueados
def remove_readonly(func, path, excinfo):
    """Intenta eliminar el atributo de solo lectura y vuelve a intentar"""
    os.chmod(path, stat.S_IWRITE)
    func(path)

# Función para desplegar nuevos archivos con opciones de configuración
def desplegar_archivos(origen, destino, eliminar, reemplazar_webconfig, 
                      reemplazar_appsettings, reemplazar_appsettings_prod):
    try:
        ARCHIVOS_CONFIG = {
            'web.config': reemplazar_webconfig,
            'appsettings.json': reemplazar_appsettings,
            'appsettings.production.json': reemplazar_appsettings_prod
        }

        # 1. Validación de rutas
        if not os.path.exists(origen):
            return False, f"Ruta origen no existe: {origen}"
        if not os.path.exists(destino):
            return False, f"Ruta destino no existe: {destino}"

        # 2. Eliminación de contenido en destino si está activado
        if eliminar:
            for item in os.listdir(destino):
                item_path = os.path.join(destino, item)

                # Evitar eliminar archivos de configuración que no deben reemplazarse
                if item in ARCHIVOS_CONFIG and not ARCHIVOS_CONFIG[item]:
                    continue

                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, onerror=remove_readonly)
                else:
                    try:
                        os.remove(item_path)
                    except PermissionError:
                        os.chmod(item_path, stat.S_IWRITE)
                        os.remove(item_path)

        # 3. Copia de nuevos archivos desde el origen
        for root, dirs, files in os.walk(origen):
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), origen)
                destino_path = os.path.join(destino, rel_path)

                # Omitir archivos de configuración si no deben reemplazarse
                if file in ARCHIVOS_CONFIG and not ARCHIVOS_CONFIG[file]:
                    continue

                os.makedirs(os.path.dirname(destino_path), exist_ok=True)
                shutil.copy2(os.path.join(root, file), destino_path)

        return True, "Despliegue exitoso"
    
    except Exception as e:
        return False, f"Error en despliegue: {str(e)}"
# Función principal de despliegue
def ejecutar_despliegue(selected, change_number, backup_dir, config_sitios):
    global global_progress, global_results, global_backup_path, despliegue_activo, despliegue_completado_global
    
    try:
        despliegue_activo = True
        despliegue_completado_global = False
        global_progress = []
        global_results = []
        
        if not selected or not change_number or not backup_dir or not config_sitios:
            global_progress.append("-> Error: Configuración incompleta")
            despliegue_completado_global = True
            return
        
        # Crear directorio base de backup
        fecha_actual = datetime.now().strftime("%d%m%Y")
        backup_path_base = os.path.join(backup_dir, f"{change_number}-{fecha_actual}", "Backup")
        os.makedirs(backup_path_base, exist_ok=True)
        global_progress.append(f"-> Directorio de backup creado: {backup_path_base}")
        global_backup_path = backup_path_base
        
        for site_app in selected:
            parts = site_app.split('/')
            sitio = parts[0]
            app = parts[1] if len(parts) > 1 else ''
            nombre_completo = site_app
            
            # Obtener configuración específica para este sitio
            config = config_sitios.get(site_app, {})
            eliminar = config.get('eliminar', False)
            reemplazar_webconfig = config.get('reemplazar_webconfig', False)
            reemplazar_appsettings = config.get('reemplazar_appsettings', False)
            reemplazar_appsettings_prod = config.get('reemplazar_appsettings_prod', False)
            new_files_dir = config.get('new_files_dir', '')
            
            # Obtener ruta física
            global_progress.append(f"-> Obteniendo ruta física para {nombre_completo}")
            ruta_fisica = obtener_ruta_fisica(sitio, app)
            if not ruta_fisica or not os.path.exists(ruta_fisica):
                global_progress.append(f"-> Error: Ruta física no encontrada para {nombre_completo}")
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error',
                    'mensaje': f"Ruta física no encontrada: {ruta_fisica}"
                })
                continue
            
            # Calcular ruta relativa para el backup
            unidad = os.path.splitdrive(ruta_fisica)[0]
            ruta_relativa = os.path.relpath(ruta_fisica, unidad + os.sep)
            
            # Paso 1: Backup
            global_progress.append(f"-> Iniciando backup para {nombre_completo}")
            success, mensaje_backup = hacer_backup(ruta_fisica, backup_path_base, ruta_relativa)
            if not success:
                global_progress.append(f"-> Error en backup para {nombre_completo}: {mensaje_backup}")
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error en backup',
                    'mensaje': mensaje_backup
                })
                continue
            global_progress.append(f"-> Backup completado para {nombre_completo}")
            
            # Obtener pool asociado
            pool_name = obtener_pool_del_sitio(sitio)
            if not pool_name:
                global_progress.append(f"-> Error: No se pudo obtener el pool para el sitio {sitio}")
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error',
                    'mensaje': f"No se pudo obtener el pool para el sitio {sitio}"
                })
                continue
            
            # Guardar estados iniciales
            estado_inicial_sitio = obtener_estado_sitio(sitio)
            estado_inicial_pool = obtener_estado_pool(pool_name)
            
            global_progress.append(f"-> Estado Inicial sitio: {sitio} = {estado_inicial_sitio}")
            global_progress.append(f"-> Estado Inicial pool: {pool_name} = {estado_inicial_pool}")
            
            # Paso 2: Detener sitio si está iniciado
            global_progress.append(f"-> Deteniendo sitio: {sitio}")
            success, mensaje_detener_sitio = detener_sitio(sitio)
            if not success:
                global_progress.append(f"-> Error deteniendo sitio: {sitio} - {mensaje_detener_sitio}")
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error deteniendo sitio',
                    'mensaje': mensaje_detener_sitio
                })
                continue
            global_progress.append(f"-> {mensaje_detener_sitio}")
            
            # Paso 3: Detener pool si está iniciado
            global_progress.append(f"-> Deteniendo pool: {pool_name}")
            success, mensaje_detener_pool = detener_pool(pool_name)
            if not success:
                global_progress.append(f"-> Error deteniendo pool: {pool_name} - {mensaje_detener_pool}")
                # Intentar reiniciar el sitio a pesar del error
                iniciar_sitio(sitio, estado_inicial_sitio)
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error deteniendo pool',
                    'mensaje': mensaje_detener_pool
                })
                continue
            global_progress.append(f"-> {mensaje_detener_pool}")
            
            # Esperar 3 segundos para asegurar que los recursos se liberen
            time.sleep(3)
            
            # Paso 4: Desplegar nuevos archivos
            global_progress.append(f"-> Desplegando nuevos archivos en {nombre_completo}")
            success, mensaje_despliegue = desplegar_archivos(
                new_files_dir, 
                ruta_fisica, 
                eliminar,
                reemplazar_webconfig,
                reemplazar_appsettings,
                reemplazar_appsettings_prod
            )
            if not success:
                global_progress.append(f"-> Error en despliegue para {nombre_completo}: {mensaje_despliegue}")
                # Intentar restaurar estado inicial
                iniciar_pool(pool_name, estado_inicial_pool)
                iniciar_sitio(sitio, estado_inicial_sitio)
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error en despliegue',
                    'mensaje': mensaje_despliegue
                })
                continue
            global_progress.append(f"-> Despliegue completado para {nombre_completo}")
            
            # Paso 5: Restaurar estado inicial del pool
            global_progress.append(f"-> Restaurando estado inicial del pool: {pool_name}")
            success, mensaje_pool = iniciar_pool(pool_name, estado_inicial_pool)
            if not success:
                global_progress.append(f"-> Error restaurando pool: {pool_name} - {mensaje_pool}")
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error restaurando pool',
                    'mensaje': mensaje_pool
                })
            else:
                global_progress.append(f"-> {mensaje_pool}")
            
            # Paso 6: Restaurar estado inicial del sitio
            global_progress.append(f"-> Restaurando estado inicial del sitio: {sitio}")
            success, mensaje_sitio = iniciar_sitio(sitio, estado_inicial_sitio)
            if not success:
                global_progress.append(f"-> Error restaurando sitio: {sitio} - {mensaje_sitio}")
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Error restaurando sitio',
                    'mensaje': mensaje_sitio
                })
            else:
                global_progress.append(f"-> {mensaje_sitio}")
                global_results.append({
                    'nombre': nombre_completo,
                    'estado': 'Completado',
                    'mensaje': "Despliegue exitoso"
                })
        
        global_progress.append("-> Proceso de despliegue finalizado")
        despliegue_completado_global = True
    
    except Exception as e:
        global_progress.append(f"-> Error crítico: {str(e)}")
        despliegue_completado_global = True
    
    finally:
        despliegue_activo = False

# Rutas de la aplicación web
@app.route('/')
def index():
    return redirect(url_for('config_general'))

@app.route('/seleccionar', methods=['POST'])
def seleccionar():
    selected = request.form.getlist('sites')
    session['selected_sites'] = selected
    return redirect(url_for('confirmar'))

@app.route('/volver')
def volver():
    return redirect(url_for('seleccionar_sitios'))

# Nueva ruta para configuración general
@app.route('/config_general', methods=['GET', 'POST'])
def config_general():
    if request.method == 'POST':
        session['change_number'] = request.form['change_number']
        session['backup_dir'] = request.form['backup_dir']
        return redirect(url_for('seleccionar_sitios'))  # ← Va a la pantalla de sitios
    return render_template('config_general.html')

@app.route('/seleccionar_sitios')
def seleccionar_sitios():
    sites = get_iis_sites()
    selected_sites = session.get('selected_sites', [])
    return render_template('index.html', sites=sites, selected_sites=selected_sites)

@app.route('/confirmar')
def confirmar():
    selected = session.get('selected_sites', [])
    if not selected:
        return redirect(url_for('index'))
    
    sites_info = []
    for site_app in selected:
        parts = site_app.split('/')
        sitio = parts[0]
        app = parts[1] if len(parts) > 1 else ''
        nombre_completo = site_app
        ruta_fisica = obtener_ruta_fisica(sitio, app)
        sites_info.append({
            'nombre': nombre_completo,
            'ruta': ruta_fisica
        })
    
    return render_template('confirm.html', sites=sites_info)

# Ruta para configuración por sitio
@app.route('/config_sitios', methods=['POST'])
def config_sitios():
    global despliegue_activo
    
    # Recoger configuración por sitio
    config_sitios = {}
    selected = session.get('selected_sites', [])
    
    for site_app in selected:
        # Obtener opciones de configuración
        eliminar = request.form.get(f'eliminar_{site_app}') == '1'
        reemplazar_webconfig = request.form.get(f'reemplazar_webconfig_{site_app}') == '1'
        reemplazar_appsettings = request.form.get(f'reemplazar_appsettings_{site_app}') == '1'
        reemplazar_appsettings_prod = request.form.get(f'reemplazar_appsettings_prod_{site_app}') == '1'
        new_files_dir = request.form.get(f'new_files_{site_app}', '')
        
        config_sitios[site_app] = {
            'eliminar': eliminar,
            'reemplazar_webconfig': reemplazar_webconfig,
            'reemplazar_appsettings': reemplazar_appsettings,
            'reemplazar_appsettings_prod': reemplazar_appsettings_prod,
            'new_files_dir': new_files_dir
        }
    
    # Obtener datos generales de la sesión
    change_number = session.get('change_number', '')
    backup_dir = session.get('backup_dir', '')
    
    # Iniciar el despliegue en un hilo separado con los datos
    thread = threading.Thread(
        target=ejecutar_despliegue,
        args=(selected, change_number, backup_dir, config_sitios)
    )
    thread.start()
    
    return redirect(url_for('progreso'))

@app.route('/progreso')
def progreso():
    return render_template('progreso.html')

@app.route('/get_progress')
def get_progress():
    return jsonify({
        'progress': global_progress,
        'active': despliegue_activo,
        'completed': despliegue_completado_global
    })

@app.route('/resultados')
def resultados():
    return render_template('resultados.html', 
                          resultados=global_results, 
                          backup_path=global_backup_path,
                          progreso=global_progress)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)