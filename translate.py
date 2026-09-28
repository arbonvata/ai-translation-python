import sys
from pathlib import Path

def get_string_xml_files(folder_path):
    target_dir = Path(folder_path)
    
    if not target_dir.is_dir():
        print(f"Error: The path '{target_dir}' is not a valid directory.")
        sys.exit(1)
        
    matching_files = []
    
    # rglob searches recursively. Use glob('*.xml') if you don't want subfolders.
    for file_path in target_dir.rglob('*.xml'):
        if file_path.is_file():
            filename = file_path.name
            if 'string' in filename.lower():
                matching_files.append(filename)
                
    return matching_files

if __name__ == "__main__":
    # Check if the user provided a folder path in the terminal
    if len(sys.argv) < 2:
        print("Usage: python3 find_xml.py <folder_path>")
        print("Example: python3 find_xml.py ./res/values")
        sys.exit(1)
        
    # Get the folder path from the first command-line argument
    folder_to_search = sys.argv[1]
    
    files = get_string_xml_files(folder_to_search)
    
    if not files:
        print("No matching files found.")
    else:
        for f in files:
            print(f)