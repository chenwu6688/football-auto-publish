#!/usr/bin/env python3
"""中文实体映射层 —— 球员层生成器（计划 13.1）

从 football-data.org 六大联赛/欧冠射手榜拉取球员名单，套用人工 curated 中文译名
与认知度分级，合并进 config/entity_map.json。

依赖：FOOTBALL_DATA_KEY 环境变量（见 scripts/local_env.sh.example）
用法：
    export FOOTBALL_DATA_KEY=xxx
    python3 scripts/build_player_map.py            # 增量刷新（当日缓存）
    python3 scripts/build_player_map.py --refresh  # 强制重新拉取

分级口径：
  A = 泛球迷也认识，中文媒体译名唯一稳定 → 必进新闻流
  B = 主流联赛知名球员，中文媒体有固定译名 → 进新闻流
  C = 知名度过低 / 译名不稳定 → confidence=low，不进标题生成，只作背景数据
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EMAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"
CACHE_DIR = PROJECT_ROOT / "data" / "fixtures" / "scorers"

API = "https://api.football-data.org/v4"
COMPETITIONS = {"PL": 2021, "PD": 2014, "SA": 2019, "BL1": 2002, "FL1": 2015, "CL": 2001}
COMP_ZH = {"PL": "英超", "PD": "西甲", "SA": "意甲", "BL1": "德甲", "FL1": "法甲", "CL": "欧冠"}
SECTION_ZH = {"Offence": "前锋", "Midfield": "中场", "Defence": "后卫", "Goalkeeper": "门将"}
CONF = {"A": "high", "B": "medium", "C": "low"}

# ── curated 中文译名表：name_en|awareness|name_zh|alias1|alias2... ──
CURATED_ROWS = """
Kylian Mbappé|A|姆巴佩|基利安·姆巴佩|姆总
Erling Haaland|A|哈兰德|艾尔林·哈兰德|魔人布欧
Jude Bellingham|A|贝林厄姆|祖德·贝林厄姆
Vinicius Junior|A|维尼修斯|维尼修斯·儒尼奥尔|小熊
Lamine Yamal|A|亚马尔|拉明·亚马尔
Raphinha|A|拉菲尼亚|拉斐尔·迪亚斯
Harry Kane|A|凯恩|哈里·凯恩
Kevin De Bruyne|A|德布劳内|凯文·德布劳内|KDB
Lautaro Martínez|A|劳塔罗·马丁内斯|劳塔罗
Bukayo Saka|A|萨卡|布卡约·萨卡
Cole Palmer|A|帕尔默|科尔·帕尔默
Bruno Fernandes|A|布鲁诺·费尔南德斯|B费|布鲁诺
Alexander Isak|A|伊萨克|亚历山大·伊萨克
Ousmane Dembélé|A|奥斯曼·登贝莱|登贝莱
Marquinhos|A|马尔基尼奥斯
Michael Olise|A|奥利塞|迈克尔·奥利塞
Jamal Musiala|A|穆西亚拉|贾马尔·穆西亚拉
Pedri|A|佩德里
Federico Valverde|A|巴尔韦德|费德里科·巴尔韦德
Luis Díaz|A|路易斯·迪亚斯|迪亚斯
Nicolò Barella|A|巴雷拉|尼科洛·巴雷拉
Enzo Fernández|A|恩佐·费尔南德斯|恩佐
Alphonso Davies|A|阿方索·戴维斯|戴维斯
Joško Gvardiol|A|格瓦迪奥尔|约什科·格瓦迪奥尔
Dayot Upamecano|A|于帕梅卡诺|达约·于帕梅卡诺
Nico Williams|A|尼科·威廉姆斯
Christian Pulisic|A|普利西奇|克里斯蒂安·普利西奇|美国队长
Christopher Nkunku|A|恩昆库|克里斯托弗·恩昆库
Cody Gakpo|A|加克波|科迪·加克波
Gianluca Scamacca|A|斯卡马卡|詹卢卡·斯卡马卡
Gabriel Jesus|A|热苏斯|加布里埃尔·热苏斯
João Cancelo|A|坎塞洛|若昂·坎塞洛
Gonçalo Ramos|A|贡萨洛·拉莫斯
Bruno Guimarães|A|布鲁诺·吉马良斯|吉马良斯
Alexis Mac Allister|A|麦卡利斯特|亚历克西斯·麦卡利斯特
Fabián Ruiz|A|法比安·鲁伊斯|法比安
Hakan Çalhanoğlu|A|恰尔汗奥卢|哈坎·恰尔汗奥卢
Marcus Thuram-Ulien|A|马库斯·图拉姆|图拉姆
Martin Ødegaard|A|厄德高|马丁·厄德高
Kai Havertz|A|哈弗茨|凯·哈弗茨
Arda Guler|A|居莱尔|阿尔达·居莱尔
Kang-in Lee|A|李刚仁
Olivier Giroud|A|吉鲁|奥利维耶·吉鲁
Dominik Szoboszlai|A|索博斯洛伊|多米尼克·索博斯洛伊
Marc Guéhi|A|格伊|马克·格伊
Nico Schlotterbeck|A|施洛特贝克|尼科·施洛特贝克
Vitinha|A|维蒂尼亚
Ademola Lookman|A|卢克曼|阿德莫拉·卢克曼
Álvaro Carreras|A|阿尔瓦罗·卡雷拉斯|卡雷拉斯
Donyell Malen|B|马伦|唐耶尔·马伦
Ante Budimir|B|布迪米尔|安特·布迪米尔
Pierre-Emerick Aubameyang|B|奥巴梅扬|皮埃尔-埃梅里克·奥巴梅扬
Amine Gouiri|B|古伊里|阿明·古伊里
Fermín López|B|费尔明·洛佩斯|费尔明
Ferrán Torres|B|费兰·托雷斯
Niclas Füllkrug|B|菲尔克鲁格|尼克拉斯·菲尔克鲁格
Patrik Schick|B|希克|帕特里克·希克
Yuito Suzuki|B|铃木唯人
Álex Baena|B|巴埃纳|亚历克斯·巴埃纳
Brian Brobbey|B|布罗比|布莱恩·布罗比
Daniel Maldini|B|丹尼尔·马尔蒂尼
Davide Frattesi|B|弗拉泰西|达维德·弗拉泰西
Ermedin Demirovic|B|德米罗维奇|埃尔梅丁·德米罗维奇
Florian Thauvin|B|托万|弗洛里安·托万
Franco Mastantuono|B|马斯塔努奥诺|佛朗哥·马斯塔努奥诺
Jonathan Burkardt|B|布尔卡特|约纳坦·布尔卡特
Jonathan David|B|乔纳森·戴维|戴维
João Pedro|B|若昂·佩德罗
Kevin Schade|B|沙德|凯文·沙德
Mariano Díaz|B|马里亚诺·迪亚斯|马里亚诺
Morgan Rogers|B|摩根·罗杰斯|罗杰斯
Pascal Groß|B|格罗斯|帕斯卡尔·格罗斯
Rayan Cherki|B|谢尔基|拉扬·谢尔基
Adam Hložek|B|赫洛热克|亚当·赫洛热克
Adrien Rabiot|B|拉比奥|阿德里安·拉比奥
Alberto Moleiro|B|莫莱罗|阿尔贝托·莫莱罗
Alejandro Grimaldo|B|格里马尔多|亚历杭德罗·格里马尔多
Ander Barrenetxea|B|巴雷内切亚|安德尔·巴雷内切亚
Anthony Elanga|B|埃兰加|安东尼·埃兰加
Antoine Semenyo|B|塞梅尼奥|安托万·塞梅尼奥
Antonio Nusa|B|努萨|安东尼奥·努萨
Assane Diao|B|阿萨内·迪奥|迪奥
Ayase Ueda|B|上田绮世
Bartra|B|巴尔特拉|马克·巴尔特拉
Bremer|B|布雷默
Bryan Mbeumo|B|姆贝乌莫|布赖恩·姆贝乌莫
Can Uzun|B|詹·乌尊|乌尊
Cucho Hernández|B|库乔·埃尔南德斯
Dominic Calvert-Lewin|B|卡尔弗特-勒温|勒温
Edon Zhegrova|B|热格罗瓦|埃东·热格罗瓦
Felix Kalu Nmecha|B|恩梅查|费利克斯·恩梅查
Francesco Esposito|B|弗朗切斯科·埃斯波西托
Fábio Silva|B|法比奥·席尔瓦
Georges Mikautadze|B|米卡乌塔泽|乔治·米卡乌塔泽
Gerard Moreno|B|赫拉德·莫雷诺
Giuliano Simeone|B|朱利亚诺·西蒙尼
Joe Willock|B|威洛克|乔·威洛克
Joshua King|B|约书亚·金
Karim Adeyemi|B|阿德耶米|卡里姆·阿德耶米
Manu Koné|B|科内|马努·科内
Matias Soule|B|苏莱|马蒂亚斯·苏莱
Mbwana Samatta|B|萨马塔|姆布瓦纳·萨马塔
Niccolo Pisilli|B|皮西利|尼科洛·皮西利
Nicolas Pépé|B|佩佩|尼古拉斯·佩佩
Rasmus Højlund|B|霍伊伦|拉斯穆斯·霍伊伦
Ridle Baku|B|巴库|里德勒·巴库
Rodrigo Riquelme|B|里克尔梅|罗德里戈·里克尔梅
Sehrou Guirassy|B|吉拉西|塞鲁·吉拉西
Thierno Barry|B|蒂耶尔诺·巴里
Tyrick Mitchell|B|米切尔|泰里克·米切尔
Abdessamad Ezzalzouli|B|埃扎尔祖利|阿卜杜萨马德·埃扎尔祖利
Ainsley Maitland-Niles|B|梅特兰-奈尔斯|安斯利·梅特兰-奈尔斯
Albert Guðmundsson|B|古德蒙德松|阿尔贝特·古德蒙德松
Aleksandar Pavlović|B|帕夫洛维奇|亚历山大·帕夫洛维奇
Angel Gomes|B|安赫尔·戈麦斯
Anton Stach|B|斯塔赫|安东·斯塔赫
Artem Dovbyk|B|多夫比克|阿尔乔姆·多夫比克
Ben Chilwell|B|奇尔维尔|本·奇尔维尔
Benjamin Šeško|B|舍什科|本亚明·舍什科
Bilal El Khannouss|B|埃尔哈努斯|比拉尔·埃尔哈努斯
Billy Gilmour|B|吉尔莫|比利·吉尔莫
Bryan Cristante|B|克里斯坦特|布赖恩·克里斯坦特
Canales|B|卡纳莱斯|塞尔希奥·卡纳莱斯
Carlos Augusto Lobaton|B|卡洛斯·奥古斯托
Conor Gallagher|B|加拉格尔|康纳·加拉格尔
Corentin Tolisso|B|托利索|科朗坦·托利索
Cristian Volpato|B|沃尔帕托|克里斯蒂安·沃尔帕托
Dan Ndoye|B|恩多耶|丹·恩多耶
Domenico Berardi|B|贝拉尔迪|多梅尼科·贝拉尔迪
Emiliano Buendía|B|布恩迪亚|埃米利亚诺·布恩迪亚
Eric Dier|B|戴尔|埃里克·戴尔
Fabio Carvalho|B|法比奥·卡瓦略
Facundo Buonanotte|B|布奥纳诺特|法昆多·布奥纳诺特
Federico Bernardeschi|B|贝尔纳代斯基|费德里科·贝尔纳代斯基
Federico Gatti|B|加蒂|费德里科·加蒂
Florian Neuhaus|B|诺伊豪斯|弗洛里安·诺伊豪斯
Francisco Conceição|B|孔塞桑|弗朗西斯科·孔塞桑
Giacomo Raspadori|B|拉斯帕多里|贾科莫·拉斯帕多里
Gio Reyna|B|雷纳|吉奥·雷纳
Habib Diallo|B|哈比卜·迪亚洛
Harvey Barnes|B|哈维·巴恩斯
Iago Aspas|B|阿斯帕斯|亚戈·阿斯帕斯
Idrissa Guèye|B|伊德里萨·盖耶
Ismael Saibari|B|赛巴里|伊斯梅尔·赛巴里
Iñaki Williams|B|伊纳基·威廉姆斯
Jacob Ramsey|B|拉姆齐|雅各布·拉姆齐
James Tarkowski|B|塔科夫斯基|詹姆斯·塔科夫斯基
John McGinn|B|麦金|约翰·麦金
Justin Kluivert|B|克鲁伊维特|贾斯汀·克鲁伊维特
Jørgen Strand Larsen|B|斯特兰德·拉森|约根·斯特兰德·拉森
Kiernan Dewsbury Hall|B|杜斯伯里-霍尔|基尔南·杜斯伯里-霍尔
Lazar Samardzic|B|萨马尔季奇|拉扎尔·萨马尔季奇
Lewis Dunk|B|邓克|刘易斯·邓克
Lewis Hall|B|刘易斯·霍尔
Liam Delap|B|德拉普|利亚姆·德拉普
Lisandro Martínez|B|利桑德罗·马丁内斯|利马
Loïs Openda|B|奥彭达|洛伊斯·奥彭达
Luis Suárez|B|路易斯·苏亚雷斯
Malick Fofana|B|马利克·福法纳
Marcos Llorente|B|马科斯·略伦特
Mario Hermoso|B|埃尔莫索|马里奥·埃尔莫索
Matheus Cunha|B|库尼亚|马特乌斯·库尼亚
Matteo Politano|B|波利塔诺|马特奥·波利塔诺
Mattia Zaccagni|B|扎卡尼|马蒂亚·扎卡尼
Maximilian Beier|B|拜尔|马克西米利安·拜尔
Maximilian Mittelstädt|B|米特尔施泰特|马克西米利安·米特尔施泰特
Miguel Gutiérrez|B|米格尔·古铁雷斯
Mohammed Amoura|B|阿莫拉|穆罕默德·阿莫拉
Morgan Gibbs-White|B|吉布斯-怀特|摩根·吉布斯-怀特
Nico Paz|B|尼科·帕斯
Nicolas Jackson|B|杰克逊|尼古拉斯·杰克逊
Nicolás González|B|尼古拉斯·冈萨雷斯
Noah Okafor|B|奥卡福|诺亚·奥卡福
Oihan Sancet|B|桑塞特|奥伊汉·桑塞特
Pedro Neto|B|内托|佩德罗·内托
Pierre Emile Højbjerg|B|霍伊别尔|皮埃尔-埃米尔·霍伊别尔
Pietro Comuzzo|B|科穆佐|彼得罗·科穆佐
Piotr Zieliński|B|泽林斯基|彼得·泽林斯基
Roberto Piccoli|B|皮科利|罗伯托·皮科利
Robin Gosens|B|戈森斯|罗宾·戈森斯
Robin Le Normand|B|勒诺曼|罗宾·勒诺曼
Rodrigo Mora|B|罗德里戈·莫拉
Rolando Mandragora|B|曼德拉戈拉|罗兰多·曼德拉戈拉
Romeo Lavia|B|拉维亚|罗密欧·拉维亚
Sebastian Szymański|B|希曼斯基|塞巴斯蒂安·希曼斯基
Sem Steijn|B|斯泰因|塞姆·斯泰因
Sepe Elye Wahi|B|埃利耶·瓦希
Sergiño Dest|B|德斯特|塞尔吉尼奥·德斯特
Shuto Machino|B|町野修斗
Stanislav Lobotka|B|洛博特卡|斯坦尼斯拉夫·洛博特卡
Teun Koopmeiners|B|科普梅纳斯|特恩·科普梅纳斯
Thomas Lemar|B|勒马尔|托马·勒马尔
Troy Parrott|B|帕罗特|特洛伊·帕罗特
Umar Sadiq|B|乌马尔·萨迪克
Valentin Rongier|B|龙吉耶|瓦朗坦·龙吉耶
Willi Orban|B|奥尔班|威利·奥尔班
Yangel Herrera|B|扬赫尔·埃雷拉
Yann Aurel Bisseck|B|比塞克|扬·奥雷尔·比塞克
Yoane Wissa|B|维萨|约安·维萨
Yussuf Poulsen|B|波尔森|尤素福·波尔森
Álex Berenguer|B|贝伦格尔|亚历克斯·贝伦格尔
Éderson|B|埃德森
Sergio Camello|C|塞尔吉奥·卡梅略
Roberto Fernández|C|罗伯托·费尔南德斯
Yassir Zabiri|C|亚西尔·扎比里
Fer Niño|C|费尔·尼诺
Antonio Raimondo|C|安东尼奥·赖蒙多
Gustavo Varela|C|古斯塔沃·巴雷拉
Kamory Doumbia|C|卡莫里·敦比亚
Lassine Sinayoko|C|拉辛·西纳约科
Lucas Boyé|C|卢卡斯·博耶
Paris Brunner|C|帕里斯·布鲁纳
Phillip Tietz|C|菲利普·蒂茨
Younes Ebnoutalib|C|尤尼斯·埃布努塔利卜
Adil Bourabaa|C|阿迪尔·布拉巴
Brugui|C|布鲁吉
Cameron Archer|C|卡梅伦·阿彻
Casper Tengstedt|C|卡斯珀·滕斯泰特
Diego Moreira|C|迭戈·莫雷拉
Ernest Nuamah|C|欧内斯特·努阿马
Esteban Lepaul|C|埃斯特班·勒波尔
Giorgi Kvernadze|C|乔治·克韦尔纳泽
Igor Matanovic|C|伊戈尔·马塔诺维奇
Louis Mafouta|C|路易·马富塔
Luka Sučić|C|卢卡·苏契奇
Marcus Tavernier|C|马库斯·塔弗尼耶
Maurice Krattenmacher|C|莫里斯·克拉滕马赫
Michael Gregoritsch|C|米夏埃尔·格雷戈里奇
Pape Gueye|C|帕佩·盖耶
Vasilije Adzic|C|瓦西里耶·阿吉奇
Adam Daghim|C|亚当·达吉姆
Adrián|C|阿德里安
Alphadjo Cisse|C|阿尔法乔·西塞
Anastasios Douvikas|C|阿纳斯塔西奥斯·杜维卡斯
Carlos Espí|C|卡洛斯·埃斯皮
Charalampos Kostoulas|C|哈拉兰博斯·科斯图拉斯
Che Adams|C|奇·亚当斯
Christian Kofane|C|克里斯蒂安·科法内
Danijel Sturm|C|达尼耶尔·施图尔姆
David Mokwa Ntusu|C|大卫·莫克瓦·恩图苏
Emersonn|C|埃默森
Franjo Ivanovic|C|弗兰约·伊万诺维奇
Gessime Yassine|C|热西姆·亚辛
Hassane Kamara|C|哈桑·卡马拉
Hugo Bolin|C|雨果·博林
Iván Romero|C|伊万·罗梅罗
Jack Hinshelwood|C|杰克·欣谢尔伍德
Jayden Bogle|C|杰登·博格勒
Josha Vagnoman|C|约沙·瓦格诺曼
José Romero|C|何塞·罗梅罗
Jurgen Ekkelenkamp|C|尤尔根·埃克伦坎普
Lassana Coulibaly|C|拉萨纳·库利巴利
Leif Davis|C|莱夫·戴维斯
Malick Yalcouyé|C|马利克·亚尔库耶
Marco Grüll|C|马尔科·格吕尔
Martin Baturina|C|马丁·巴图里纳
Mateo Pellegrino|C|马特奥·佩莱格里诺
Matthieu Udol|C|马蒂厄·乌多尔
Maximilian Eggestein|C|马克西米利安·埃格施泰因
Miguel Ángel Sierra|C|米格尔·安赫尔·谢拉
Milutin Osmajić|C|米卢廷·奥斯马伊奇
Mohamed Belloumi|C|穆罕默德·贝卢米
Pablo Duran|C|巴勃罗·杜兰
Pablo García|C|巴勃罗·加西亚
Roberto Navarro|C|罗伯托·纳瓦罗
Robin Fellhauer|C|罗宾·费尔豪尔
Samuel Amo-Ameyaw|C|塞缪尔·阿莫-阿梅亚乌
Sheraldo Becker|C|谢拉尔多·贝克尔
Thijs Dallinga|C|泰斯·达林加
Thomas Jørgensen|C|托马斯·约根森
Tiago Gabriel|C|蒂亚戈·加布里埃尔
Tiago Santos|C|蒂亚戈·桑托斯
Tijjani Noslin|C|蒂贾尼·诺斯林
Tim Skarke|C|蒂姆·斯卡克
Vitaly Janelt|C|维塔利·雅内尔特
Yannik Engelhardt|C|扬尼克·恩格尔哈特
Zachary Athekame|C|扎卡里·阿特卡梅
Álvaro García|C|阿尔瓦罗·加西亚
Abdallah Sima|C|阿卜杜拉·西马
Abiel Osorio|C|阿别尔·奥索里奥
Adil Aouchiche|C|阿迪尔·奥希什
Adrien Thomasson|C|阿德里安·托马松
Adrià Pedrosa|C|阿德里亚·佩德罗萨
Adrián de la Fuente|C|阿德里安·德拉富恩特
Aitor Paredes Casamichana|C|艾托尔·帕雷德斯
Akor Adams|C|阿科尔·亚当斯
Alessandro Romano|C|亚历山德罗·罗马诺
Alex Scott|C|亚历克斯·斯科特
Alexis Claude Maurice|C|亚历克西斯·克劳德-莫里斯
Alexsandro Ribeiro|C|亚历山德罗·里贝罗
Alysson|C|阿利松
Amine El Ouazzani|C|阿明·埃尔瓦扎尼
Amine Sbaï|C|阿明·斯巴伊
Anan Khalaili|C|阿南·哈拉伊利
Andrea Colpani|C|安德烈亚·科尔帕尼
Andrei Rațiu|C|安德烈·拉齐乌
Andrija Maksimović|C|安德里亚·马克西莫维奇
Andrés Martín|C|安德烈斯·马丁
Ange-Yoan Bonny|C|昂热-约安·博尼
Antoine Hainaut|C|安托万·埃诺
Antoine Mille|C|安托万·米尔
Anton Kade|C|安东·卡德
Antonio Vergara|C|安东尼奥·贝尔加拉
Archie Brown|C|阿奇·布朗
Arijon Ibrahimovic|C|阿里永·伊布拉希莫维奇
Bazoumana Touré|C|巴祖马纳·图雷
Branco van den Boomen|C|布兰科·范登博门
Buba|C|布巴
Calvin Brackelmann|C|卡尔文·布拉克尔曼
Carl Starfelt|C|卡尔·斯塔费尔特
Carlens Arcus|C|卡伦斯·阿库斯
Carlos Dotor|C|卡洛斯·多托尔
Cas Odenthal|C|卡斯·奥登塔尔
Chema Andrés|C|切马·安德烈斯
Chidera Ejuke|C|奇德拉·埃尤克
Christ Tapé|C|克里斯特·塔佩
Chuba Akpom|C|丘巴·阿克波姆
Chupete|C|丘佩特
Cole Campbell|C|科尔·坎贝尔
Conrad Harder|C|康拉德·哈德
César Palacios|C|塞萨尔·帕拉西奥斯
Dame Guèye|C|达梅·盖耶
Daniel Namaso|C|丹尼尔·纳马索
Daniel Svensson|C|丹尼尔·斯文松
Danny da Costa|C|丹尼·达科斯塔
Dariusz Stalmach|C|达留什·斯塔尔马赫
Derry Scherhant|C|德里·舍尔汉特
Dilane Bakwa|C|迪拉内·巴夸
Diogo Lobão|C|迪奥戈·洛班
Emmanuel Latte Lath|C|埃马纽埃尔·拉特·拉特
Enes Ünal|C|埃内斯·于纳尔
Enzo Bardeli|C|恩佐·巴尔德利
Enzo Le Fée|C|恩佐·勒费
Eren Dinkci|C|埃伦·丁克奇
Eric Martel|C|埃里克·马特尔
Ethan Mbappé|C|埃唐·姆巴佩
Exequiel Zeballos|C|埃塞基耶尔·塞巴略斯
Fabian Rieder|C|法比安·里德尔
Facundo Medina|C|法昆多·梅迪纳
Felix Bacher|C|费利克斯·巴赫尔
Felix Keidel|C|费利克斯·凯德尔
Ferran Jutglà|C|费兰·胡特格拉
Florian Sotoca|C|弗洛里安·索托卡
Flávio Nazinho|C|弗拉维奥·纳济尼奥
Gabriele Bracaglia|C|加布里埃莱·布拉卡利亚
Geny Catamo|C|热尼·卡塔莫
Giacomo Calò|C|贾科莫·卡洛
Gleiker Mendoza|C|格莱克尔·门多萨
Gonzalo García|C|贡萨洛·加西亚
Grischa Prömel|C|格里沙·普罗梅尔
Harouna Djibirin|C|哈鲁纳·吉比林
Hugo Vetlesen|C|雨果·韦特勒森
Hákon Haraldsson|C|哈康·哈拉尔德松
Ibrahim Maza|C|易卜拉欣·马扎
Ibrahima Baldé|C|易卜拉希马·巴尔德
Igor Jesus|C|伊戈尔·热苏斯
Ilan Kebbal|C|伊兰·凯巴尔
Ioannis Konstantelias|C|约安尼斯·康斯坦泰利亚斯
Isaac|C|伊萨克
Isac Lidberg|C|伊萨克·利德贝里
Isak Jensen|C|伊萨克·延森
Jacen Russell-Rowe|C|杰森·拉塞尔-罗
Jack Clarke|C|杰克·克拉克
Jacobo Ortega|C|哈科沃·奥尔特加
Jacobo Ramón|C|哈科沃·拉蒙
Jaidon Anthony|C|杰登·安东尼
Jakub Piotrowski|C|雅库布·彼得罗夫斯基
James Abankwah|C|詹姆斯·阿班夸
Jan Paul van Hecke|C|扬·保罗·范赫克
Jay Dasilva|C|杰·达席尔瓦
Jay Robinson|C|杰·罗宾逊
Jean-Victor Makengo|C|让-维克托·马肯戈
Jesper Karlström|C|耶斯佩尔·卡尔斯特伦
Johan Manzambi|C|约翰·曼赞比
Johan Vásquez|C|约翰·巴斯克斯
John Yeboah|C|约翰·耶博阿
Jon Guridi|C|琼·古里迪
Jon Olasagasti|C|琼·奥拉萨加斯蒂
Joseph Scally|C|约瑟夫·斯卡利
Josh Doig|C|乔什·多伊格
Josh Maja|C|乔什·马加
Juan Iglesias|C|胡安·伊格莱西亚斯
Jérémy Le Douaron|C|热雷米·勒杜阿龙
Kaiki Bruno|C|凯基·布鲁诺
Keane Lewis-Potter|C|基恩·刘易斯-波特
Keyliane Abdallah|C|凯利安·阿卜杜拉
Khalis Merah|C|哈利斯·梅拉
Kieron Bowie|C|基伦·鲍伊
Kike Barja|C|基克·巴尔哈
Lamine Sy|C|拉明·西
Lennart Karl|C|伦纳特·卡尔
Leon Avdullahu|C|莱昂·阿夫杜拉胡
Leopold Querfeld|C|利奥波德·奎尔费尔德
Liam Millar|C|利亚姆·米勒
Linton Maina|C|林顿·迈纳
Lucas Calodat|C|卢卡斯·卡洛达
Lucas Maronnier|C|卢卡斯·马罗尼耶
Lucas Stassin|C|卢卡斯·斯塔辛
Ludovic Ajorque|C|吕多维克·阿若克
Ludovit Reis|C|卢多维特·赖斯
Luis Rioja|C|路易斯·里奥哈
Luismi Cruz|C|路易斯米·克鲁斯
Luka Vušković|C|卢卡·武什科维奇
Lukas Petkov|C|卢卡斯·佩特科夫
Maguette Gueye|C|马盖特·盖耶
Mamadou Koné|C|马马杜·科内
Marc Aguado|C|马克·阿瓜多
Marc Pubill|C|马克·普比尔
Marcos Fernández|C|马科斯·费尔南德斯
Martin Satriano|C|马丁·萨特里亚诺
Matteo Cancellieri|C|马特奥·坎切列里
Maxim De Cuyper|C|马克西姆·德凯佩尔
Maximilian Wöber|C|马克西米利安·韦伯
Maximo Perrone|C|马克西莫·佩罗内
Michael Kayode|C|迈克尔·卡约德
Mikel Rodríguez|C|米克尔·罗德里格斯
Mitchell Weiser|C|米切尔·魏泽
Mohamed Cho|C|穆罕默德·绍
Nahuel Tenaglia|C|纳韦尔·特纳利亚
Natan|C|纳坦
Nicolo Tresoldi|C|尼科洛·特雷索尔迪
Nicolás Capaldo|C|尼古拉斯·卡帕尔多
Nicolás Ezequiel Fernández|C|尼古拉斯·费尔南德斯
Nikola Krstović|C|尼古拉·克尔斯托维奇
Nilson Angulo|C|尼尔松·安古洛
Noah Mbamba|C|诺亚·姆班巴
Noah Nartey|C|诺亚·纳尔泰
Nobel Mendy|C|诺贝尔·门迪
Ochieng|C|奥奇恩
Olaf Gorter|C|奥拉夫·戈特
Orri Oskarsson|C|奥里·奥斯卡松
Oso|C|奥索
Ozan Kabak|C|奥赞·卡巴克
Pablo Ibáñez Tébar|C|巴勃罗·伊巴涅斯
Pablo Pagis|C|巴勃罗·帕吉
Pathé Mboup|C|帕泰·姆布普
Paul Mendy|C|保罗·门迪
Peque|C|佩克
Philipp Mwene|C|菲利普·姆韦内
Pierre Ganiou|C|皮埃尔·加尼乌
Pontus Almqvist|C|蓬图斯·阿尔姆奎斯特
Prosper Peter|C|普罗斯珀·彼得
Przemysław Frankowski|C|普热梅斯瓦夫·弗兰科夫斯基
Ragnar Ache|C|拉格纳·阿赫
RamónTerrats Espacio|C|拉蒙·特拉茨
Ransford Königsdörffer|C|兰斯福德·柯尼希斯德费尔
Rayan Fofana|C|拉扬·福法纳
Renaud Ripart|C|勒诺·里帕尔
Ricardo Mangas|C|里卡多·曼加斯
Richie Sagrado|C|里奇·萨格拉多
Rocco Reitz|C|罗科·赖茨
Rodrigo Ribeiro|C|罗德里戈·里贝罗
Rodrigo Zalazar|C|罗德里戈·萨拉萨尔
Romain Del Castillo|C|罗曼·德尔卡斯蒂略
Romulo Cruz|C|罗穆洛·克鲁兹
Ruben Aguilar|C|鲁本·阿吉拉尔
Rémy Labeau Lascary|C|雷米·拉博·拉斯卡里
Răzvan Marin|C|勒兹万·马林
Said El Mala|C|赛义德·埃尔马拉
Samir Chergui|C|萨米尔·谢尔吉
Sandro Kulenović|C|桑德罗·库莱诺维奇
Santiago Castaneda|C|圣地亚哥·卡斯塔涅达
Santiago Mouriño|C|圣地亚哥·穆里尼奥
Saud Abdulhamid|C|沙特·阿卜杜勒哈米德
Sebastiano Esposito|C|塞巴斯蒂亚诺·埃斯波西托
Semi Ajayi|C|塞米·阿贾伊
Sergio Martínez|C|塞尔吉奥·马丁内斯
Simone Lontani|C|西莫内·隆塔尼
Stanis Idumbo-Muzambo|C|斯塔尼斯·伊敦博-穆赞博
Stefano Marino|C|斯特凡诺·马里诺
Steffen Tigges|C|斯特芬·蒂格斯
Suleiman Camara Sanneh|C|苏莱曼·卡马拉
Thiago|C|蒂亚戈
Tidiam Gomis|C|蒂迪亚姆·戈米斯
Tim Lemperle|C|蒂姆·伦佩勒
Tyrhys Dolan|C|泰里斯·多兰
Tyrique George|C|泰里克·乔治
Ville Koski|C|维莱·科斯基
Vincent Marchetti|C|樊尚·马尔凯蒂
Víctor Muñoz|C|维克托·穆尼奥斯
Wilson Isidor|C|威尔逊·伊西多尔
Xavi Espart|C|哈维·埃斯帕特
Yasin Ayari|C|亚辛·阿亚里
Zakaria Eddahchouri|C|扎卡里亚·埃达舒里
Zian Flemming|C|齐安·弗莱明
Zlatko Tripić|C|兹拉特科·特里皮奇
Álex Calatrava|C|亚历克斯·卡拉特拉瓦
Álex Jiménez|C|亚历克斯·希门尼斯
"""


def load_curated():
    out = {}
    for line in CURATED_ROWS.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        out[parts[0]] = {
            "name_zh": parts[2],
            "awareness": parts[1],
            "aliases": [a for a in parts[3:] if a and a != parts[2]],
        }
    return out


def fetch_scorers(refresh=False):
    """拉取六赛事射手榜（当日缓存）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    key = os.environ.get("FOOTBALL_DATA_KEY")
    raw = {}
    for code in COMPETITIONS:
        cache = CACHE_DIR / f"{code}_{today}.json"
        if cache.exists() and not refresh:
            raw[code] = json.load(open(cache, encoding="utf-8"))
            continue
        if not key:
            if cache.exists():
                raw[code] = json.load(open(cache, encoding="utf-8"))
                continue
            sys.exit("❌ 缺少 FOOTBALL_DATA_KEY，且无当日缓存")
        import urllib.request
        req = urllib.request.Request(
            f"{API}/competitions/{COMPETITIONS[code]}/scorers?limit=100",
            headers={"X-Auth-Token": key},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            raw[code] = json.loads(r.read().decode())
        json.dump(raw[code], open(cache, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"   拉取 {COMP_ZH[code]}: {len(raw[code].get('scorers', []))} 人")
        time.sleep(7)   # 免费层 10 次/分
    return raw


def build_players(raw):
    curated = load_curated()
    acc = {}
    for code, d in raw.items():
        for s in d.get("scorers", []):
            p = s.get("player", {})
            n = p.get("name")
            if not n:
                continue
            r = acc.setdefault(n, {
                "name_en": n, "fd_player_id": p.get("id"), "section": p.get("section"),
                "team_en": "", "comps": [], "goals": 0,
            })
            team = (s.get("team") or {}).get("name", "")
            if team and not r["team_en"]:
                r["team_en"] = team
            if code not in r["comps"]:
                r["comps"].append(code)
            r["goals"] = max(r["goals"], s.get("goals", 0) or 0)

    out, missing = {}, []
    for n, r in acc.items():
        c = curated.get(n)
        if not c:
            missing.append(n)
            c = {"name_zh": n, "awareness": "C", "aliases": []}
        slug = "player_" + re.sub(r"[^a-z0-9_]", "", n.lower().replace(" ", "_"))
        out[slug] = {
            "internal_id": slug, "fd_id": r["fd_player_id"], "af_id": None,
            "name_en": n, "name_zh": c["name_zh"], "aliases": c["aliases"],
            "awareness": c["awareness"], "confidence": CONF[c["awareness"]],
            "category": "player",
            "position": SECTION_ZH.get(r["section"] or "", r["section"]),
            "team_zh": "", "team_en": r["team_en"],
            "competitions": [COMP_ZH.get(x, x) for x in r["comps"]],
            "season_goals": r["goals"],
        }
    return out, missing


def merge_into_entity_map(players):
    emap = json.load(open(EMAP_PATH, encoding="utf-8"))
    team_by_en = {v["name_en"].lower(): v["name_zh"] for v in emap["entities"].values()
                  if v["category"] == "club"}
    for v in emap["entities"].values():
        if v["category"] == "club":
            for a in v.get("aliases", []):
                team_by_en.setdefault(a.lower(), v["name_zh"])

    unmatched = {}
    for p in players.values():
        t = team_by_en.get((p["team_en"] or "").lower())
        p["team_zh"] = t or ""
        if not t and p["team_en"]:
            unmatched[p["team_en"]] = unmatched.get(p["team_en"], 0) + 1

    # 只替换球员层，球队层原样保留
    emap["entities"] = {k: v for k, v in emap["entities"].items() if v["category"] != "player"}
    emap["entities"].update(players)

    allv = list(emap["entities"].values())
    emap["stats"] = {
        "total": len(allv),
        "clubs": sum(1 for v in allv if v["category"] == "club"),
        "players": sum(1 for v in allv if v["category"] == "player"),
        "A": sum(1 for v in allv if v["awareness"] == "A"),
        "B": sum(1 for v in allv if v["awareness"] == "B"),
        "C": sum(1 for v in allv if v["awareness"] == "C"),
        "low_confidence": sum(1 for v in allv if v.get("confidence") == "low"),
    }
    emap["version"] = "2.0"
    emap["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    json.dump(emap, open(EMAP_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return emap["stats"], unmatched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略当日缓存，重新拉取")
    args = ap.parse_args()

    print("📡 拉取射手榜…")
    raw = fetch_scorers(refresh=args.refresh)
    players, missing = build_players(raw)
    stats, unmatched = merge_into_entity_map(players)

    print(f"\n✅ 实体映射表：{stats['total']} 个"
          f"（球队 {stats['clubs']} / 球员 {stats['players']}）")
    print(f"   认知度：A={stats['A']} B={stats['B']} C={stats['C']}"
          f"，其中 low_confidence={stats['low_confidence']}")
    if missing:
        print(f"⚠️  curated 未覆盖 {len(missing)} 人（按 C 级占位）："
              f"{', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")
    if unmatched:
        print(f"⚠️  {len(unmatched)} 个球队未在映射表中（球员 team_zh 留空）")


if __name__ == "__main__":
    main()
