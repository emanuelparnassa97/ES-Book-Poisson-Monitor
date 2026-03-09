# ES-Book-Poisson-Monitor
מערכת לניטור סיכוי הפריצה לטיק הבא בבוק על החוזה של השוק באמצעות התפלגות פואסון המודדות כמות אורדרים ממוצעת אל מול אינטרוול נבחר ואת שינוי קצב הגעתם (למבדא)
יש להתקין קודם את קובץ orderflow exporter בתיקיית sierra charts ACS Source
ולאחר מכן את קובץ orderflow_exporter בתיקיית data
לאחר מכן להיכנס לסיירה -> analysis ואז build advanced costom studies
לאחר מכן visual c++ Path לבחור בתיקיית x64 בתיקיית microsoft visual studio
בfiles to compile לבחור orderflowexporter.cpp
בadditional compiler parameters לכתוב c:/sierrachart/acs_source
ללחוץ build release
ואז כרגיל להכנס לאחד הצ'ארטים להוסיף סטאדי
להפעיל cmd ולהעתיק את הפקודה: python C:\SierraChart\Data\orderflow_analyzer.py 
