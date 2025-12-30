#!/usr/bin/env python
# coding: utf-8

import json
import os
import time
import logging
from datetime import datetime
from openalex_authors_multifield import process_single_field

# Configuração de logging
def setup_logging():
    """Configura o sistema de logging para cada campo"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f"{log_dir}/batch_controller_{timestamp}.log"),
            logging.StreamHandler()
        ]
    )

def load_field_configs():
    """Carrega as configurações dos campos a partir do arquivo JSON"""
    with open('config/campos_config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config["campos"]

def create_field_logger(field_name):
    """Cria um logger específico para um campo"""
    logger = logging.getLogger(field_name.replace(" ", "_").lower())
    logger.setLevel(logging.INFO)
    
    # Evita adicionar handlers múltiplas vezes
    if not logger.handlers:
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        # Handler para arquivo específico do campo
        file_handler = logging.FileHandler(f"logs/{field_name.replace(' ', '_').lower()}_log.txt")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        # Handler para console
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    return logger

def run_batch_collection():
    """Executa a coleta em lote para todos os campos configurados"""
    setup_logging()
    logger = logging.getLogger("batch_controller")
    
    field_configs = load_field_configs()
    
    logger.info(f"Iniciando coleta em lote para {len(field_configs)} campos")
    
    completed_fields = []
    failed_fields = []
    
    for i, field_config in enumerate(field_configs):
        field_name = field_config["nome"]
        field_logger = create_field_logger(field_name)
        
        logger.info(f"[{i+1}/{len(field_configs)}] Iniciando processamento para {field_name}")
        field_logger.info(f"Iniciando processamento para {field_name}")
        
        try:
            # Executa a coleta para este campo
            process_single_field(field_config)
            
            completed_fields.append(field_name)
            logger.info(f"✅ Concluído: {field_name}")
            field_logger.info(f"✅ Processamento concluído com sucesso")
            
            # Pausa entre campos para respeitar limites de taxa
            if i < len(field_configs) - 1:
                logger.info("⏳ Pausa entre campos...")
                field_logger.info("⏳ Pausa entre campos...")
                time.sleep(10)  # Pausa de 10 segundos entre campos
                
        except KeyboardInterrupt:
            logger.error(f"🛑 Interrompido pelo usuário durante o processamento de {field_name}")
            field_logger.error("🛑 Processamento interrompido pelo usuário")
            failed_fields.append(field_name)
            break
        except Exception as e:
            error_msg = f"❌ Erro ao processar {field_name}: {str(e)}"
            logger.error(error_msg)
            field_logger.error(f"❌ Erro: {str(e)}")
            failed_fields.append(field_name)
            continue
    
    # Gera relatório final
    logger.info("="*60)
    logger.info("RELATÓRIO FINAL")
    logger.info("="*60)
    logger.info(f"Campos processados com sucesso: {len(completed_fields)}")
    logger.info(f"Campos com falha: {len(failed_fields)}")
    
    if completed_fields:
        logger.info("Campos concluídos:")
        for field in completed_fields:
            logger.info(f"  - {field}")
    
    if failed_fields:
        logger.info("Campos com falha:")
        for field in failed_fields:
            logger.info(f" - {field}")
    
    logger.info("="*60)
    
    # Gera arquivo de resumo
    summary = {
        "timestamp": datetime.now().isoformat(),
        "total_fields": len(field_configs),
        "completed": completed_fields,
        "failed": failed_fields,
        "success_rate": len(completed_fields) / len(field_configs) * 100
    }
    
    with open(f"logs/batch_summary_{timestamp}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Resumo salvo em: logs/batch_summary_{timestamp}.json")

def run_single_field(field_name):
    """Executa a coleta para um único campo específico"""
    setup_logging()
    logger = logging.getLogger("batch_controller")
    
    field_configs = load_field_configs()
    
    # Procura pelo campo específico
    target_field = None
    for config in field_configs:
        if config["nome"].lower() == field_name.lower():
            target_field = config
            break
    
    if not target_field:
        logger.error(f"Campo '{field_name}' não encontrado na configuração")
        return False
    
    field_logger = create_field_logger(field_name)
    
    logger.info(f"Iniciando processamento para {field_name}")
    field_logger.info(f"Iniciando processamento para {field_name}")
    
    try:
        process_single_field(target_field)
        
        logger.info(f"✅ Concluído: {field_name}")
        field_logger.info(f"✅ Processamento concluído com sucesso")
        
        return True
    except Exception as e:
        error_msg = f"❌ Erro ao processar {field_name}: {str(e)}"
        logger.error(error_msg)
        field_logger.error(f"❌ Erro: {str(e)}")
        
        return False

def main():
    """Função principal - permite escolher entre execução em lote ou para campo específico"""
    import sys
    
    if len(sys.argv) > 1:
        # Execução para campo específico
        field_name = sys.argv[1]
        print(f"Executando coleta para o campo: {field_name}")
        run_single_field(field_name)
    else:
        # Execução em lote
        print("Executando coleta em lote para todos os campos configurados")
        run_batch_collection()

if __name__ == "__main__":
    main()